"""Rollback-only relation form capabilities from the owner's live snapshot."""

from sqlalchemy import select

from app.core.exceptions import AuthorizationException, NotFoundException
from app.core.field_policy.contracts import limit
from app.db.authorization_plan_session import _order_error
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.schemas.relation_form import RelationFormQuery
from app.modules.system.schemas.relation_projection import (
    MENU_DETAIL_FIELDS,
    ROLE_DETAIL_FIELDS,
)
from app.modules.system.service.authorization_read_budget import (
    authorization_read_phase,
    read_phase,
)
from app.modules.system.service.relation_capabilities import relation_capabilities
from app.modules.system.service.relation_read_context import load_relation_read_context
from app.modules.system.service.relation_read_service import (
    local_parent_links,
    project_leaders,
)
from app.modules.system.service.role_management_service import role_management_service

FORM_TYPES = {
    "role": (Role, "role_id"),
    "dept": (Dept, "dept_id"),
    "menu": (Menu, "menu_id"),
}


async def read_relation_form(db, tx, *, tenant, actor_user_version, surface, query):
    if type(query) is not RelationFormQuery or surface not in FORM_TYPES:
        raise _order_error()
    model, anchor = FORM_TYPES[surface]
    record_id = int(query.record_id) if query.record_id is not None else None
    parent_id = int(query.parent_id) if query.parent_id is not None else None
    obj = "system." + surface
    context = await load_relation_read_context(
        db,
        tx,
        tenant=tenant,
        actor_user_version=actor_user_version,
        action=surface + "." + query.mode,
        objects={obj} | ({"system.user"} if surface == "dept" else set()),
        write_form=True,
    )
    if query.mode == "update" and f"system:{surface}:list" not in context.permissions:
        raise AuthorizationException("缺少操作权限", error_code="MISSING_PERMISSION")
    values = None
    complete = {}
    if query.mode == "update":
        role = None
        if surface == "role":
            with authorization_read_phase(db, "delegation"):
                role = await role_management_service.authorize_role_projection(
                    db,
                    actor_user_id=tenant.actor_user_id,
                    role_id=record_id,
                    tenant=tenant,
                )
        entries = await context.fetch(
            db,
            model,
            anchor,
            predicates=(model.__table__.c[anchor] == record_id,),
            maximum=1,
            names=ROLE_DETAIL_FIELDS
            if surface == "role"
            else MENU_DETAIL_FIELDS | {"query", "permission"}
            if surface == "menu"
            else None,
            internals=("leader_user_id", "leader_has_text")
            if surface == "dept"
            else (),
        )
        if not entries:
            raise NotFoundException(surface)
        record, values, _ = entries[0]
        if role is not None and context.decisions(record)["dept_ids"].allowed:
            limit(len(role.depts) <= 8192)
            values["dept_ids"] = [str(d.dept_id) for d in role.depts]
        if surface == "dept":
            await project_leaders(db, context, entries)
            await local_parent_links(db, context, entries)
        if surface == "menu":
            complete["buttons"], buttons = await menu_buttons(db, context, record_id)
            if complete["buttons"]:
                values["buttons"] = buttons
    else:
        roles = context.scopes.keys()
        if surface == "dept":
            if parent_id:
                parents = await context.fetch(
                    db,
                    Dept,
                    anchor,
                    predicates=(Dept.dept_id == parent_id,),
                    maximum=1,
                    names={"dept_name"},
                )
                if not parents or "dept_name" not in parents[0][1]:
                    raise NotFoundException("dept")
                roles = parents[0][0].role_ids
            else:
                roles = [
                    rid for rid, scope in context.scopes.items() if scope.unbounded
                ]
        record = context.record(obj, "new", roles)
    return {
        "record": values,
        "field_policy": relation_capabilities(
            context, record, surface, complete=complete
        ),
    }


@read_phase("source")
async def menu_buttons(db, context, record_id):
    # Compare all actual children with the projected complete set. Never
    # offer a hidden/partial collection as a replacement snapshot.
    ids = (
        (
            await db.execute(
                select(Menu.menu_id)
                .where(
                    Menu.tenant_id == context.tenant.tenant_id,
                    Menu.parent_id == record_id,
                    Menu.menu_type == "F",
                )
                .order_by(Menu.menu_id)
                .limit(8193)
            )
        )
        .scalars()
        .all()
    )
    limit(len(ids) <= 8192)
    buttons = []
    for start in range(0, len(ids), 256):
        buttons.extend(
            await context.fetch(
                db,
                Menu,
                "menu_id",
                predicates=(Menu.menu_id.in_(ids[start : start + 256]),),
                order=(Menu.menu_id,),
                maximum=256,
                names={"menu_name", "permission"},
            )
        )
    complete = len(buttons) == len(ids) and all(
        type(row.get("menu_name")) is str and type(row.get("permission")) is str
        for _, row, _ in buttons
    )
    result = None
    if complete:
        result = [
            {"desc": row["menu_name"], "code": row["permission"]}
            for _, row, _ in buttons
        ]
        for button_record, _, _ in buttons:
            context.source(button_record, ("menu_name", "permission"))
    return complete, result
