"""Closed source footprints for all builtins, before any model/UI copy exists."""

from sqlalchemy import and_, false, or_, select

from app.core.exceptions import AuthorizationException
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.service.delivery_source_capture import (
    capture_domain,
    capture_preflight_user,
    capture_user_query,
)
from app.modules.system.field_policy.delivery_catalog import CONTAINS, EQ, JOIN, READ
from app.modules.system.field_policy.relation_catalog import SORT
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.delivery_read_context import load_delivery_context

MODELS = {
    "system.user": User,
    "system.role": Role,
    "system.dept": Dept,
    "system.menu": Menu,
    "ai.agent": AiAgent,
}


def _error(query=False):
    return AuthorizationException(
        "所需来源字段未获授权",
        error_code="FIELD_QUERY_DENIED" if query else "FIELD_READ_DENIED",
    )


def plain_coverage(context, obj, table, name):
    allow, blocked = [], []
    from app.core.field_policy import FieldRef  # noqa: PLC0415

    for rid, scope in context.scope_sql(obj, table).items():
        rule = context.rules.index.get(
            (context.tenant.tenant_id, rid, FieldRef(obj, name), READ)
        )
        if rule is not None:
            (
                allow if rule.effect == "allow" and rule.level == "plain" else blocked
            ).append(scope.is_(True))
    return and_(or_(false(), *allow), ~or_(false(), *blocked))


async def require_sources(db, context, obj, *, readable=(), query=()):
    model = MODELS[obj]
    table = model.__table__
    if query:
        try:
            await context.prove(db, obj, table, tuple(query))
        except AuthorizationException:
            raise _error(True) from None
    if readable:
        # Opaque tool prose/confirmation facts need the original plain values.
        # A masked grant cannot authorize unmasked labels inside a JSON/text copy.
        if await db.scalar(
            select(
                select(next(iter(table.primary_key.columns)))
                .where(
                    context.base(obj, table),
                    ~and_(*(plain_coverage(context, obj, table, f) for f in readable)),
                )
                .exists()
            )
        ):
            raise _error()


async def require_tool_sources(
    db, tx, *, tenant, version, tool, args, preflight=False, user_context=None
):
    if type(preflight) is not bool:
        raise _error()
    context = await load_delivery_context(
        db, tx, tenant=tenant, version=version, action="ai.tool." + tool
    )
    reads, queries = {}, {}
    context.delivery_domains = []

    def add(obj, *names):
        reads.setdefault(obj, set()).update(names)

    def query(obj, *uses):
        queries.setdefault(obj, []).extend(uses)

    if tool.startswith("role.") or tool in {"user.role_lookup", "user.update_roles"}:
        if not tool.endswith(".count"):
            add("system.role", "role_name", "role_code", "status", "data_scope")
        if (args.get("filters") or {}).get("status") is not None:
            query("system.role", ("status", EQ))
        if "query" in args and tool in {"role.lookup", "user.role_lookup"}:
            query("system.role", ("role_name", CONTAINS), ("role_code", CONTAINS))
        if tool == "user.role_lookup":
            query("system.role", ("status", EQ), ("role_code", SORT))
        if tool == "role.menu_lookup":
            add("system.role", "menu_ids")
            add("system.menu", "menu_name", "path", "status")
            add(
                "system.menu",
                "parent_id",
                "route_name",
                "route_path",
                "permission",
                "i18n_key",
            )
            query(
                "system.menu",
                *(
                    (name, CONTAINS)
                    for name in (
                        "menu_name",
                        "route_name",
                        "route_path",
                        "permission",
                        "i18n_key",
                    )
                ),
            )
        if tool in {"role.agent_lookup", "role.update_agents"}:
            add("system.role", "agent_ids")
            add("ai.agent", "name", "description")
            if tool == "role.agent_lookup":
                query("ai.agent", ("name", CONTAINS), ("description", CONTAINS))
        if tool == "role.update_menus":
            add("system.role", "menu_ids")
            add("system.menu", "menu_name", "path", "status")
    if tool.startswith("dept.") or tool in {"user.dept_lookup", "user.update_dept"}:
        if not tool.endswith(".count"):
            add("system.dept", "dept_name", "parent_id", "ancestors", "status")
        if (args.get("filters") or {}).get(
            "status"
        ) is not None or tool == "user.dept_lookup":
            query("system.dept", ("status", EQ))
        if "query" in args:
            query(
                "system.dept",
                ("dept_name", CONTAINS),
                ("parent_id", JOIN),
                ("ancestors", JOIN),
            )
        if tool == "dept.list":
            query("system.dept", ("order_num", SORT))
    if tool == "user.create":
        # The actual default role/department are server sources too.
        add("system.role", "role_name", "role_code", "status")
        add("system.dept", "dept_name", "status")
    if preflight and tool.startswith(("role.", "dept.")):
        root = "roles" if tool.startswith("role.") else "departments"
        add("system.user", root)
        query("system.user", (root, JOIN))
    if preflight and tool.startswith("dept.") and args.get("leader"):
        add("system.user", "user_name", "nickname")
        query("system.user", ("user_name", EQ))
    for obj in sorted(reads.keys() | queries.keys()):
        await require_sources(
            db,
            context,
            obj,
            readable=tuple(sorted(reads.get(obj, ()))),
            query=tuple(dict.fromkeys(queries.get(obj, ()))),
        )
        context.delivery_domains.append(
            await capture_domain(
                db,
                context,
                obj,
                readable=tuple(sorted(reads.get(obj, ()))),
                queries=tuple(dict.fromkeys(queries.get(obj, ()))),
            )
        )
    if tool in {"role.count", "dept.count"} and not context.delivery_domains:
        context.delivery_domains.append(
            await capture_domain(
                db,
                context,
                "system.role" if tool.startswith("role.") else "system.dept",
            )
        )
    context.delivery_domains.extend(await capture_user_query(db, context, tool, args))
    if preflight:
        context.delivery_domains.extend(
            await capture_preflight_user(db, context, user_context, tool, args)
        )
    return context
