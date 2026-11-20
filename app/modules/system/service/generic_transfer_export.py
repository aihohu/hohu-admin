"""SQL-projected transfer values; full target ORM entities never cross this edge."""

from dataclasses import replace

from sqlalchemy import and_, case, select

from app.core.field_policy import (
    Access,
    FieldRef,
    FieldUse,
    RecordContext,
    compile_rules,
    query_counterexample,
)
from app.core.field_policy.contracts import limit, primitive
from app.db.base import user_depts, user_roles
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.field_policy.query_plan import query_denied
from app.modules.system.field_policy.transfer import EXPORT_COLUMNS, project_export
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service import user_export_service as exports
from app.modules.system.service.relation_read_context import load_relation_read_context
from app.modules.system.service.transfer_relation_sources import restrict_relations

READ = Access("read", "management.read")
EXPORT = Access("export", "export")


async def prove_query(db, tx, context, plan, *, full_match, _after_plan=False):
    (tx.assert_barrier if _after_plan else tx.assert_planning)(context.tenant_id)
    catalog = production_catalog()
    importing = any(d.purpose == "import" for d in plan.dependencies)
    action = "transfer.user-import" if importing else "transfer.user-export"
    record = RecordContext(
        context.tenant_id,
        "system.user",
        "query",
        frozenset(context.role_scopes),
        frozenset(catalog.actions[action].permission_codes),
    )
    uses = tuple(
        FieldUse(
            FieldRef("system.user", d.field_id),
            Access(d.operation, d.purpose, "match" if importing else d.use),
        )
        for d in plan.dependencies
    )
    if not uses:
        return
    counter = query_counterexample(
        catalog,
        compile_rules(catalog, context.generic_grants),
        record,
        action,
        uses,
        and_(
            User.tenant_id == context.tenant_id,
            *(() if full_match else context.base_predicates),
        ),
        dict(context.role_scopes),
    )
    if await db.scalar(select(select(User.user_id).where(counter).exists())):
        raise query_denied()


async def project_rows(db, tx, *, tenant, version, filters, context, fields):
    source = await load_relation_read_context(
        db,
        tx,
        tenant=tenant,
        actor_user_version=version,
        action="transfer.user-export",
        objects=frozenset({"system.user", "system.dept", "system.role"}),
    )
    table = User.__table__
    source.source_version = "transfer-relations/v1"

    def permitted(name):
        return and_(
            source.coverage("system.user", table, name, READ),
            source.coverage("system.user", table, name, EXPORT),
        )

    names = tuple(f for _, f in EXPORT_COLUMNS if f in table.c)
    columns = [User.user_id] + [
        case((permitted(n), table.c[n]), else_=None).label(n) for n in names
    ]
    columns += [
        case((permitted(n), True), else_=False).label("_" + n)
        for n in ("roles", "departments")
    ]
    predicates = []
    if filters.user_names is not None:
        predicates.append(User.user_name.in_(filters.user_names))
    for name in ("user_name", "nickname", "user_email", "user_phone"):
        if getattr(filters, name):
            predicates.append(table.c[name].contains(getattr(filters, name)))
    if filters.status:
        predicates.append(User.status == filters.status)
    if filters.dept_id:
        predicates.append(
            User.user_id.in_(
                select(user_depts.c.user_id).where(
                    user_depts.c.tenant_id == tenant.tenant_id,
                    user_depts.c.dept_id == int(filters.dept_id),
                )
            )
        )
    rows = (
        (
            await db.execute(
                select(*columns)
                .where(source.base("system.user", table), *predicates)
                .order_by(User.user_id)
                .limit(5001)
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > 5000:
        raise exports.UnprocessableEntityException(
            "超过同步导出行数上限，请缩小筛选范围",
            error_code="AI_EXPORT_ASYNC_REQUIRED",
        )
    ids = tuple(r["user_id"] for r in rows)
    if filters.user_names is not None:
        # Exact matching is already proven over the complete base. Do not read a
        # hidden account value merely to check cardinality (user_name is unique).
        if len(rows) != len(set(filters.user_names)):
            raise exports.BusinessRuleException(
                "指定账号未全部匹配当前可见范围或筛选条件",
                error_code="AI_EXPORT_TARGETS_UNAVAILABLE",
            )
    decisions = await fields.decisions(
        db, tx, context, ids, operation="export", purpose="export"
    )
    dept_links, role_links = {}, {}
    for links, column, target, allowed in (
        (user_depts, "dept_id", dept_links, "departments"),
        (user_roles, "role_id", role_links, "roles"),
    ):
        eligible = tuple(r["user_id"] for r in rows if r["_" + allowed])
        if eligible:
            for uid, key in await db.execute(
                select(links.c.user_id, links.c[column])
                .where(
                    links.c.tenant_id == tenant.tenant_id, links.c.user_id.in_(eligible)
                )
                .order_by(links.c.user_id, links.c[column])
            ):
                target.setdefault(uid, []).append(key)
    limit(
        sum(map(len, dept_links.values())) + sum(map(len, role_links.values())) <= 16384
    )
    # Follow only projected parent links. No raw ancestor path or source label is
    # loaded before its source-cell authorization has succeeded.
    departments, pending = {}, {keys[0] for keys in dept_links.values() if keys}
    for _ in range(65):
        if not pending:
            break
        limit(len(departments) + len(pending) <= 5000)
        batch = await source.fetch(
            db,
            Dept,
            "dept_id",
            predicates=(Dept.dept_id.in_(pending),),
            maximum=5001,
            names=("dept_name", "parent_id"),
        )
        departments.update(
            {int(rec.record_key): (rec, values) for rec, values, _ in batch}
        )
        pending = {
            int(v["parent_id"]) for _, v, _ in batch if v.get("parent_id")
        } - departments.keys()
    roles_needed = {key for keys in role_links.values() for key in keys}
    limit(len(roles_needed) <= 1024)
    roles = {
        int(rec.record_key): (rec, values)
        for rec, values, _ in (
            await source.fetch(
                db,
                Role,
                "role_id",
                predicates=(Role.role_id.in_(roles_needed),),
                maximum=1025,
                names=("role_code", "status"),
            )
            if roles_needed
            else ()
        )
    }
    raw, allowed, used = [], [], set()
    for row in rows:
        uid = row["user_id"]
        values = {n: row[n] for n in names}
        values["status"] = exports._STATUS_LABELS.get(
            values["status"], values["status"] or ""
        )
        values["user_gender"] = exports._GENDER_LABELS.get(
            values["user_gender"], values["user_gender"] or ""
        )
        values["create_time"] = (
            values["create_time"].strftime("%Y-%m-%d %H:%M:%S")
            if values["create_time"]
            else ""
        )
        permitted_relations = set()
        chain, key, seen = [], next(iter(dept_links.get(uid, ())), None), set()
        complete = row["_departments"]
        while key is not None:
            if key in seen or len(seen) >= 65 or key not in departments:
                complete = False
                break
            seen.add(key)
            rec, cell = departments[key]
            if not isinstance(cell.get("dept_name"), str) or "parent_id" not in cell:
                complete = False
                break
            chain.append(cell["dept_name"])
            key = int(cell["parent_id"]) if cell["parent_id"] else None
        values["departments"] = "/".join(reversed(chain))
        if complete:
            permitted_relations.add("departments")
            used.update(("dept", did) for did in seen)
        codes, valid = [], row["_roles"]
        for rid in role_links.get(uid, ()):
            cell = roles.get(rid, (None, {}))[1]
            if "status" not in cell or "role_code" not in cell:
                valid = False
                break
            if cell["status"] == "1":
                codes.append(cell["role_code"])
            used.add(("role", rid))
        values["roles"] = ",".join(codes)
        if valid:
            permitted_relations.add("roles")
        raw.append(values)
        allowed.append(permitted_relations)
    projection = restrict_relations(
        project_export(tuple(raw), tuple(decisions[uid] for uid in ids)), tuple(allowed)
    )
    for kind, key in sorted(used):
        if ("departments" if kind == "dept" else "roles") not in projection.columns:
            continue
        record, values = (departments if kind == "dept" else roles)[key]
        source.source(
            record,
            tuple(n for n in ("dept_name", "parent_id") if n in values)
            if kind == "dept"
            else tuple(n for n in ("role_code", "status") if n in values),
        )
    return replace(
        projection, related_sources=tuple(primitive(ref) for ref in source.source_refs)
    ), ids
