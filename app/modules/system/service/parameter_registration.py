"""Complete eligible registration roles and exact current Role source projection."""

from collections import defaultdict

from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.field_policy.contracts import denied
from app.modules.system.field_policy.control_catalog import UPDATE
from app.modules.system.models.role import Role
from app.modules.system.service.authorization_read_budget import (
    authorization_read_phase,
)
from app.modules.system.service.authorization_resource_catalog import resource_spec
from app.modules.system.service.grant_authority import grant_authority_service
from app.modules.system.service.parameter_context import load_parameter_context
from app.modules.system.service.parameter_facts import parameter_rows
from app.modules.system.service.relation_read_context import RelationReadContext

_FIELDS = {
    K.ROLE: ("role_id", "role_code", "status", "data_scope"),
    K.MENU: ("menu_id", "permission", "route_name"),
    K.ROLE_MENUS: ("role_id", "menu_id"),
    K.ROLE_DEPTS: ("role_id", "dept_id"),
    K.ROLE_AGENT: ("role_id", "agent_id", "enabled"),
}


async def registration_candidates(db, context):
    with authorization_read_phase(db, "delegation"):
        facts = {
            kind: await parameter_rows(
                db, resource_spec(kind).table, context.tenant.tenant_id, names=names
            )
            for kind, names in _FIELDS.items()
        }
        authority = await grant_authority_service.build(
            db, context.tenant.actor_user_id, tenant=context.tenant
        )
    menus = {row["menu_id"]: row for row in facts[K.MENU]}
    bound = defaultdict(set)
    depts = defaultdict(set)
    agents = defaultdict(set)
    for row in facts[K.ROLE_MENUS]:
        bound[row["role_id"]].add(row["menu_id"])
    for row in facts[K.ROLE_DEPTS]:
        depts[row["role_id"]].add(row["dept_id"])
    for row in facts[K.ROLE_AGENT]:
        if row["enabled"]:
            agents[row["role_id"]].add(row["agent_id"])
    candidates = []
    for role in facts[K.ROLE]:
        identifier = role["role_id"]
        if (
            role["status"] != "1"
            or role["role_code"] == "R_SUPER"
            or not bound[identifier] <= menus.keys()
        ):
            continue
        values = [menus[key] for key in bound[identifier]]
        if any(
            (menu["permission"] or "").startswith(("system:", "platform:"))
            or menu["permission"] == "*"
            or (menu["route_name"] or "").startswith(
                ("system", "tenant", "ai_provider", "ai_agent")
            )
            for menu in values
        ):
            continue
        if (
            authority.allows_permission_codes(
                {menu["permission"] for menu in values if menu["permission"]}
            )
            and authority.allows_menu_ids(bound[identifier])
            and authority.allows_scope_kind(role["data_scope"], depts[identifier])
            and authority.allows_agent_ids(agents[identifier])
        ):
            candidates.append(identifier)
    return tuple(sorted(candidates))


async def registration_options(db, tx, context, version):
    record = context.record("system.setting", "settings", context.scopes)
    if "registration_role_id" not in context.allowed(
        record, "settings.registration-options", UPDATE
    ):
        return False, None
    sources = await load_parameter_context(
        db,
        tx,
        tenant=context.tenant,
        actor_user_version=version,
        action="settings.registration-options",
    )
    candidates = await registration_candidates(db, sources)
    result = []
    with authorization_read_phase(db, "source"):
        for offset in range(0, len(candidates), 256):
            entries = await RelationReadContext.fetch(
                sources,
                db,
                Role,
                "role_id",
                predicates=(Role.role_id.in_(candidates[offset : offset + 256]),),
                maximum=256,
                names={"role_id", "role_name"},
            )
            if len(entries) != len(candidates[offset : offset + 256]) or any(
                "role_name" not in values or type(values["role_name"]) is not str
                for _, values, _ in entries
            ):
                return False, None
            for role, values, _ in entries:
                sources.source(role, ("role_id", "role_name"))
                result.append(
                    {"label": values["role_name"], "value": values["role_id"]}
                )
    return True, result


async def require_registration_choice(db, tx, context, version, role_id):
    if (
        not set(
            context.catalog.actions["settings.registration-options"].permission_codes
        )
        <= context.permissions
    ):
        raise AuthorizationException("缺少操作权限", error_code="MISSING_PERMISSION")
    record = context.record("system.setting", "settings", context.scopes)
    if "registration_role_id" not in context.allowed(
        record, "settings.registration-options", UPDATE
    ):
        raise denied()
    if not role_id:
        return
    sources = await load_parameter_context(
        db,
        tx,
        tenant=context.tenant,
        actor_user_version=version,
        action="settings.registration-options",
    )
    candidates = await registration_candidates(db, sources)
    if (
        not role_id.isascii()
        or not role_id.isdecimal()
        or str(int(role_id)) != role_id
        or int(role_id) not in candidates
    ):
        raise BusinessRuleException(
            "Registration role is not eligible or delegable",
            error_code="REGISTRATION_ROLE_INVALID",
        )
    with authorization_read_phase(db, "source"):
        entries = await RelationReadContext.fetch(
            sources,
            db,
            Role,
            "role_id",
            predicates=(Role.role_id == int(role_id),),
            maximum=1,
            names={"role_id", "role_name"},
        )
    if len(entries) != 1 or type(entries[0][1].get("role_name")) is not str:
        raise denied()
    sources.source(entries[0][0], ("role_id", "role_name"))
