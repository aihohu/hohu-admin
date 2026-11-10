"""Protected Role/Dept/Menu read adapters. No ORM leaves this module."""

from sqlalchemy import and_, func, select

from app.core.exceptions import NotFoundException
from app.core.field_policy.contracts import denied, limit, require
from app.db.authorization_plan_session import _order_error
from app.modules.system.field_policy.relation_catalog import CONTAINS, EQ, EXACT, SORT
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import DeptQuery
from app.modules.system.schemas.menu import MenuQuery
from app.modules.system.schemas.role import RoleQuery
from app.modules.system.service.relation_read_context import load_relation_read_context
from app.modules.system.service.role_management_service import role_management_service
from app.modules.system.service.user_role_assignment_service import (
    user_role_assignment_service,
)

MODELS = {
    "role": (Role, RoleQuery, "role_id", "role_code", 4096),
    "dept": (Dept, DeptQuery, "dept_id", "order_num", 8192),
    "menu": (Menu, MenuQuery, "menu_id", "order", 8192),
}


def local_tree(records, *, anchor, options=False, buttons=False):
    """Rebuild only safe local ancestry, never return database ancestor paths."""
    label = "dept_name" if anchor == "dept_id" else "menu_name"
    by_id = {r[anchor]: r for r in records}
    parents = {
        key: r.get("parent_id")
        if r.get("parent_id") in by_id and label in by_id[r["parent_id"]]
        else None
        for key, r in by_id.items()
    }
    for key, row in by_id.items():
        path, current = [], key
        while parents.get(current):
            current = parents[current]
            require(current not in path and current != key)
            path.append(current)
            limit(len(path) < 16)
        if "parent_id" in row:
            row["parent_id"] = parents[key]
        if "ancestors" in row:
            row["ancestors"] = ",".join(["0", *reversed(path)])
    if options:
        if any(label not in r for r in records):
            raise denied()
        nodes = {
            key: {
                "id": key,
                "label": row[label],
                "p_id": parents[key] or "",
                "children": [],
                **({"i18n_key": row["i18n_key"]} if "i18n_key" in row else {}),
            }
            for key, row in by_id.items()
        }
    else:
        nodes = {
            key: {**row, "children": [], **({"buttons": []} if buttons else {})}
            for key, row in by_id.items()
        }
    roots = []
    for key, row in by_id.items():
        parent = parents[key]
        if buttons and row.get("menu_type") == "F":
            if parent and "menu_name" in row and "permission" in row:
                nodes[parent]["buttons"].append(
                    {"desc": row["menu_name"], "code": row["permission"]}
                )
        elif parent:
            nodes[parent]["children"].append(nodes[key])
        else:
            roots.append(nodes[key])
    return roots


async def project_leaders(db, context, entries):
    ids = sorted(
        {
            extra["leader_user_id"]
            for _, _, extra in entries
            if extra.get("leader_user_id")
        }
    )
    sources = {}
    for i in range(0, len(ids), 256):
        rows = await context.fetch(
            db,
            User,
            "user_id",
            names={"user_name", "nickname"},
            predicates=(User.user_id.in_(ids[i : i + 256]),),
            maximum=256,
        )
        sources.update(
            {int(record.record_key): (record, values) for record, values, _ in rows}
        )
    for record, output, extra in entries:
        if "leader" not in output:
            continue
        source = sources.get(extra.get("leader_user_id"))
        if not source or not {"user_name", "nickname"} <= source[1].keys():
            output.pop("leader", None)
            continue
        source_record, values = source
        output["leader"] = values["nickname"] or values["user_name"]
        context.source(record, ("leader",))
        context.source(source_record, ("user_name", "nickname"))


async def local_parent_links(db, context, entries):
    table = Dept.__table__
    ids = {
        int(values["parent_id"]) for _, values, _ in entries if values.get("parent_id")
    }
    visible = (
        set(
            (
                await db.execute(
                    select(table.c.dept_id).where(
                        context.base("system.dept", table),
                        table.c.dept_id.in_(ids),
                        context.coverage("system.dept", table, "dept_name"),
                    )
                )
            ).scalars()
        )
        if ids
        else set()
    )
    for _, values, _ in entries:
        values.pop("ancestors", None)
        if values.get("parent_id") and int(values["parent_id"]) not in visible:
            values["parent_id"] = None


async def prove_leader_query(db, context):
    table, source = Dept.__table__, User.__table__
    valid = (
        select(1)
        .select_from(source)
        .where(
            source.c.user_id == table.c.leader_user_id,
            context.base("system.user", source),
            *(
                context.coverage("system.user", source, n, CONTAINS)
                for n in ("user_name", "nickname")
            ),
        )
        .correlate(table)
        .exists()
    )
    incomplete = and_(
        context.base("system.dept", table),
        table.c.leader.is_not(None),
        table.c.leader != "",
        ~valid,
    )
    if await db.scalar(select(select(1).select_from(table).where(incomplete).exists())):
        raise denied()


async def read_relation(
    db, tx, *, tenant, actor_user_version, operation, query=None, target_id=None
):
    if type(operation) is not str or operation.count(".") != 1:
        raise _order_error()
    surface, kind = operation.split(".")
    allowed = {
        "role": {"list", "all", "detail", "assignable", "menus", "agents"},
        "dept": {"list", "detail", "tree", "tree-list", "options"},
        "menu": {"list", "all", "tree", "tree-list", "options", "pages", "exists"},
    }
    if surface not in allowed or kind not in allowed[surface]:
        raise _order_error()
    model, schema, anchor, sort, cap = MODELS[surface]
    if kind == "list" and type(query) is not schema:
        raise _order_error()
    if kind == "tree" and surface == "dept" and type(query) is not DeptQuery:
        raise _order_error()
    if kind == "assignable" and not (
        type(query) is dict
        and set(query) <= {"query", "limit"}
        and type(query.get("limit")) is int
        and 1 <= query["limit"] <= 20
        and (
            query.get("query") is None
            or type(query["query"]) is str
            and len(query["query"]) <= 50
        )
    ):
        raise _order_error()
    if kind in {"detail", "menus", "agents"}:
        if type(target_id) is not int or not 0 < target_id < 2**63 or query is not None:
            raise _order_error()
    elif target_id is not None:
        raise _order_error()
    action = {
        "assignable": "role.assignable-read",
        "menus": "role.menus-read",
        "agents": "role.agents-read",
    }.get(kind, surface + ".list")
    objects = {"system." + surface} | ({"system.user"} if surface == "dept" else set())
    context = await load_relation_read_context(
        db,
        tx,
        tenant=tenant,
        actor_user_version=actor_user_version,
        action=action,
        objects=objects,
    )
    obj, table = "system." + surface, model.__table__
    dependencies, predicates = [], []
    if kind not in {"detail", "menus", "agents", "exists"}:
        dependencies.append((sort, SORT))
    filters = query.model_dump(exclude_none=True) if type(query) is schema else {}
    if kind in {"all", "options", "pages", "assignable"}:
        filters["status"] = "1"
    for name, value in filters.items():
        if name in {"current", "size"}:
            continue
        access = EQ if name in {"status", "data_scope", "menu_type"} else CONTAINS
        dependencies.append((name, access))
        if name == "leader":
            await prove_leader_query(db, context)
            source = User.__table__
            predicates.append(
                select(1)
                .select_from(source)
                .where(
                    source.c.tenant_id == tenant.tenant_id,
                    source.c.user_id == table.c.leader_user_id,
                    func.coalesce(
                        func.nullif(source.c.nickname, ""), source.c.user_name
                    ).contains(value, autoescape=True),
                )
                .correlate(table)
                .exists()
            )
        else:
            predicates.append(
                table.c[name] == value
                if access == EQ
                else func.pg_catalog.lower(table.c[name]).contains(
                    value.strip().casefold(), autoescape=True
                )
                if surface == "role"
                else table.c[name].contains(value, autoescape=True)
            )
    if kind == "assignable" and query and query.get("query"):
        dependencies += [("role_name", CONTAINS), ("role_code", CONTAINS)]
    if kind == "exists":
        require(type(query) is str and 0 < len(query) <= 100)
        dependencies.append(("route_name", EXACT))
        predicates.append(table.c.route_name == query)
    if kind == "pages":
        dependencies.append(("menu_type", EQ))
        predicates.append(table.c.menu_type == "C")
    await context.prove(
        db,
        obj,
        table,
        tuple(dependencies),
        readable=("role_name", "role_code")
        if kind == "assignable" or (surface == "role" and kind == "all")
        else ("dept_name" if surface == "dept" else "menu_name",)
        if kind == "options"
        else ("route_name",)
        if kind == "pages"
        else (),
    )
    if kind == "exists":
        return bool(
            await db.scalar(
                select(
                    select(1)
                    .select_from(table)
                    .where(context.base(obj, table), *predicates)
                    .exists()
                )
            )
        )
    if kind == "assignable":
        require(
            type(query) is dict
            and set(query) <= {"query", "limit"}
            and type(query.get("limit")) is int
            and 1 <= query["limit"] <= 20
        )
        count = await db.scalar(
            select(func.count()).select_from(table).where(context.base(obj, table))
        )
        limit(count <= cap)
        roles = await user_role_assignment_service.list_assignable_roles(
            db,
            actor_user_id=tenant.actor_user_id,
            query=query.get("query"),
            limit=query["limit"],
            tenant=tenant,
        )
        ids = [r.role_id for r in roles]
        entries = await context.fetch(
            db,
            Role,
            anchor,
            predicates=(Role.role_id.in_(ids),),
            order=(Role.role_code, Role.role_id),
            maximum=20,
            names={"role_code", "role_name"},
        )
        return [values for _, values, _ in entries]
    if kind in {"detail", "menus", "agents"}:
        require(type(target_id) is int and target_id > 0)
        predicates.append(table.c[anchor] == target_id)
        if surface == "role":
            role = await role_management_service.authorize_role_projection(
                db, actor_user_id=tenant.actor_user_id, role_id=target_id, tenant=tenant
            )
        entries = await context.fetch(
            db,
            model,
            anchor,
            predicates=tuple(predicates),
            maximum=1,
            internals=("leader_user_id",) if surface == "dept" else (),
        )
        if not entries:
            raise NotFoundException(surface)
        record, values, _ = entries[0]
        if surface == "role":
            decisions = context.decisions(record)
            if (
                kind in {"menus", "agents"}
                and not decisions[
                    "menu_ids" if kind == "menus" else "agent_ids"
                ].allowed
            ):
                raise denied()
            if kind == "menus":
                context.source(record, ("menu_ids",))
                limit(len(role.menus) <= 8192)
                return [
                    str(m.menu_id) for m in sorted(role.menus, key=lambda m: m.menu_id)
                ]
            if kind == "agents":
                from app.modules.ai.service.role_agent import (  # noqa: PLC0415
                    role_agent_service,
                )

                context.source(record, ("agent_ids",))
                result = await role_agent_service.get_binding(
                    db, target_id, actor_user_id=tenant.actor_user_id, tenant=tenant
                )
                return result.model_dump(mode="json", by_alias=True)
            if decisions["dept_ids"].allowed:
                values["dept_ids"] = [str(d.dept_id) for d in role.depts]
        if surface == "dept":
            await project_leaders(db, context, entries)
            await local_parent_links(db, context, entries)
        return values
    current = getattr(query, "current", 1)
    size = getattr(query, "size", 10)
    limit(size <= 100)
    total = await db.scalar(
        select(func.count())
        .select_from(table)
        .where(context.base(obj, table), *predicates)
    )
    if kind != "list":
        limit(total <= cap)
    entries = await context.fetch(
        db,
        model,
        anchor,
        predicates=tuple(predicates),
        order=(table.c[sort], table.c[anchor]),
        offset=(current - 1) * size if kind == "list" else 0,
        maximum=size if kind == "list" else cap + 1,
        names={"role_code", "role_name", "status", "data_scope", "i18n_keys"}
        if surface == "role"
        else {"menu_name", "create_time"}
        if surface == "menu" and kind == "all"
        else {"route_name"}
        if kind == "pages"
        else None,
        internals=("leader_user_id",) if surface == "dept" else (),
    )
    records = [values for _, values, _ in entries]
    if surface == "role":
        # Existing current delegation assessment remains authoritative.
        summaries, _, _ = await role_management_service.summarize_roles(
            db,
            actor_user_id=tenant.actor_user_id,
            tenant=tenant,
            **{
                k: v
                for k, v in filters.items()
                if k in {"role_name", "role_code", "status", "data_scope"}
            },
            limit=size if kind == "list" else cap + 1,
            offset=(current - 1) * size if kind == "list" else 0,
        )
        assessment = {str(r.role_id): r for r in summaries}
        for row in records:
            summary = assessment[row[anchor]]
            row.update(
                delegable=summary.delegable,
                blocked_reason_code=summary.blocked_reason_code,
            )
    if surface == "dept":
        await project_leaders(db, context, entries)
    if kind == "pages":
        for record, _output, _ in entries:
            context.source(record, ("route_name",))
        require(
            all(type(r.get("route_name")) is str and r["route_name"] for r in records)
        )
        return [r["route_name"] for r in records]
    if kind in {"tree", "tree-list", "options"}:
        records = local_tree(
            records,
            anchor=anchor,
            options=kind == "options",
            buttons=surface == "menu" and kind == "tree-list",
        )
    elif surface == "dept":
        await local_parent_links(db, context, entries)
    if kind in {"list", "tree-list"}:
        return {
            "records": records,
            "total": len(records) if kind == "tree-list" else total,
            "current": current,
            "size": size if kind == "list" else len(records),
        }
    return records
