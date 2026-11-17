"""Presentation hints from the current immutable rules and per-record scope."""

from app.core.field_policy import resolve_fields
from app.core.rbac import is_super_admin
from app.modules.auth.permission_collect import collect_user_permission_codes
from app.modules.system.field_policy.relation_catalog import CREATE, READ, UPDATE
from app.modules.system.schemas.dept import DeptCreate, DeptUpdate
from app.modules.system.schemas.menu import MenuCreate, MenuUpdate
from app.modules.system.schemas.role import RoleCreate, RoleUpdate

FORM_SCHEMAS = {
    "role": (RoleCreate, RoleUpdate),
    "dept": (DeptCreate, DeptUpdate),
    "menu": (MenuCreate, MenuUpdate),
}


def relation_write_permissions(actor, catalog, surface):
    """Match the existing writer owner, including its specific super-role rule."""
    if surface != "menu":
        # System owners check GrantAuthority.permission_codes explicitly.
        return frozenset(collect_user_permission_codes(actor))
    # Configuration owners retain read_identity's own functional gate.
    if is_super_admin(actor):
        return frozenset(
            p for action in catalog.actions.values() for p in action.permission_codes
        )
    return frozenset(
        menu.permission
        for role in actor.roles
        if role.status == "1"
        for menu in role.menus
        if menu.status == "1" and menu.permission
    )


def allowed_fields(context, record, action, access):
    permissions = (
        relation_write_permissions(
            context.actor, context.catalog, record.object_id.removeprefix("system.")
        )
        if action.endswith((".create", ".update"))
        else context.permissions
    )
    if not set(context.catalog.actions[action].permission_codes) <= permissions:
        return []
    return sorted(
        n
        for n, d in resolve_fields(
            context.catalog, context.rules, record, action, access
        ).items()
        if d.allowed
    )


def relation_capabilities(context, record, surface, *, complete=None):
    create_type, update_type = FORM_SCHEMAS[surface]
    create = allowed_fields(context, record, surface + ".create", CREATE)
    update = allowed_fields(context, record, surface + ".update", UPDATE)
    ordinary = {
        f.field_id
        for f in context.catalog.objects[record.object_id].fields
        if CREATE in f.accesses or UPDATE in f.accesses
    }
    complete = complete or {}
    special_create = sorted(set(create_type.model_fields) - ordinary)
    special_update = sorted(set(update_type.model_fields) - ordinary)
    permissions = relation_write_permissions(context.actor, context.catalog, surface)
    if not complete.get("buttons", True):
        special_update = [n for n in special_update if n != "buttons"]
    if (
        not set(context.catalog.actions[surface + ".create"].permission_codes)
        <= permissions
    ):
        special_create = []
    if (
        not set(context.catalog.actions[surface + ".update"].permission_codes)
        <= permissions
    ):
        special_update = []
    return {
        "read": allowed_fields(context, record, context.action, READ),
        "read_levels": {
            name: decision.level for name, decision in context.decisions(record).items()
        },
        "query": [],
        "create": create,
        "update": update,
        "special_create": special_create,
        "special_update": special_update,
        "can_create": bool(record.role_ids)
        and set(create_type.model_fields) & ordinary <= set(create),
        "complete": complete,
    }
