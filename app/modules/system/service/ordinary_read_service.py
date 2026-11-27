"""SQL query proofs and sparse DTO projection under a shared tenant barrier."""

from sqlalchemy import and_, case, false, func, or_, select

from app.core.field_policy import (
    FieldRef,
    FieldUse,
    RecordContext,
    query_counterexample,
)
from app.core.field_policy.contracts import denied, limit
from app.db.authorization_plan_session import _order_error
from app.modules.system.field_policy.ordinary_catalog import (
    CONTAINS,
    CREATE,
    EQ,
    EXACT,
    SORT,
    SURFACES,
    UPDATE,
    production_catalog,
)
from app.modules.system.models.data_scope_demo import DataScopeDemo
from app.modules.system.models.dict_data import DictData
from app.modules.system.models.dict_type import DictType
from app.modules.system.schemas.data_scope_demo import DataScopeDemoQuery
from app.modules.system.schemas.dict_data import DictDataQuery
from app.modules.system.schemas.dict_type import DictTypeQuery
from app.modules.system.service.data_scope_demo_service import data_scope_demo_service
from app.modules.system.service.ordinary_field_context import (
    load_ordinary_context,
    read_coverage,
)

MODELS = {
    "dict-type": (DictType, DictTypeQuery),
    "dict-data": (DictData, DictDataQuery),
    "data-scope-demo": (DataScopeDemo, DataScopeDemoQuery),
}


async def read_ordinary(db, tx, *, tenant, actor_user_version, operation, query=None):
    if type(operation) is not str or operation.count(".") != 1:
        raise _order_error()
    surface, action = operation.split(".")
    if (
        surface not in MODELS
        or action not in {"list", "options"}
        or (surface == "data-scope-demo" and action == "options")
    ):
        raise _order_error()
    model, schema = MODELS[surface]
    if action == "list" and type(query) is not schema:
        raise _order_error()
    if action == "options" and (
        (surface == "dict-type" and query is not None)
        or (
            surface == "dict-data"
            and (type(query) is not str or not 1 <= len(query) <= 100)
        )
    ):
        raise _order_error()
    context = await load_ordinary_context(
        db,
        tx,
        tenant=tenant,
        actor_user_version=actor_user_version,
        surface=surface,
        operation=action,
    )
    catalog, table = production_catalog(), model.__table__
    object_id, anchor = SURFACES[surface]
    scopes = context.scope_sql(table)
    base = and_(table.c.tenant_id == tenant.tenant_id, or_(false(), *scopes.values()))
    full = RecordContext(
        tenant.tenant_id, object_id, "query", frozenset(scopes), context.permissions
    )
    dependencies, predicates = [], []
    filters = (
        query.model_dump(exclude_none=True) if action == "list" else {"status": "1"}
    )
    if action == "options" and surface == "dict-data":
        filters["dict_type"] = query
    for name, value in filters.items():
        if name in {"current", "size"}:
            continue
        access = EQ if name == "status" else EXACT if action == "options" else CONTAINS
        dependencies.append(FieldUse(FieldRef(object_id, name), access))
        predicates.append(
            table.c[name].contains(value, autoescape=True)
            if access == CONTAINS
            else table.c[name] == value
        )
    order = [table.c[anchor].asc()]
    if surface == "dict-data":
        dependencies.append(FieldUse(FieldRef(object_id, "dict_sort"), SORT))
        order.insert(0, table.c.dict_sort.asc())
    if dependencies:
        counterexample = query_counterexample(
            catalog,
            context.rules,
            full,
            context.action,
            tuple(dependencies),
            base,
            scopes,
        )
        if await db.scalar(
            select(select(table.c[anchor]).where(counterexample).exists())
        ):
            raise denied()
    # Prove option pair readability across the original scope before returning
    # any option. Filtering out hidden pairs would disclose a misleading subset.
    if action == "options":
        pair = (
            ("dict_name", "dict_type")
            if surface == "dict-type"
            else ("dict_label", "dict_value")
        )
        coverage = and_(
            *(read_coverage(context, scopes, FieldRef(object_id, f)) for f in pair)
        )
        if await db.scalar(
            select(select(table.c[anchor]).where(base, ~coverage).exists())
        ):
            raise denied()
    fields = catalog.objects[object_id].fields
    columns = [
        case(
            (
                read_coverage(context, scopes, FieldRef(object_id, f.field_id)),
                table.c[f.field_id],
            ),
            else_=None,
        ).label(f.field_id)
        for f in fields
    ]
    roles = tuple(scopes)
    columns.extend(
        case((scopes[r], True), else_=False).label(f"_role_{i}")
        for i, r in enumerate(roles)
    )
    statement = select(*columns).where(base, *predicates).order_by(*order)
    if action == "list":
        total = await db.scalar(
            select(func.pg_catalog.count(1)).select_from(table).where(base, *predicates)
        )
        statement = statement.offset((query.current - 1) * query.size).limit(query.size)
    else:
        statement = statement.limit(1001)
    rows = (await db.execute(statement)).mappings().all()
    limit(action != "options" or len(rows) <= 1000)
    records = []
    for row in rows:
        record = RecordContext(
            tenant.tenant_id,
            object_id,
            str(row[anchor]),
            frozenset(r for i, r in enumerate(roles) if row[f"_role_{i}"]),
            context.permissions,
        )
        projected = context.output(record, row)
        if action == "list":
            projected["field_policy"] = {
                "update": context.capabilities(record, "update", UPDATE)
            }
        records.append(projected)
    if action == "options":
        return records
    # Headers describe the page's actual projection. Per-role unions would
    # advertise columns denied by another role applicable to the same record.
    readable = sorted(
        {name for row in records for name in row if name != "field_policy"}
    )
    queryable = []
    for field in fields:
        deps = tuple(
            FieldUse(FieldRef(object_id, field.field_id), a)
            for a in field.accesses
            if a.purpose == "management.query"
        )
        if not deps:
            continue
        counter = query_counterexample(
            catalog, context.rules, full, context.action, deps, base, scopes
        )
        if not await db.scalar(select(select(table.c[anchor]).where(counter).exists())):
            queryable.append(field.field_id)
    proposed = {"create_by": context.actor.user_id}
    if surface == "data-scope-demo":
        proposed["dept_id"] = await data_scope_demo_service._get_primary_dept_id(
            db, context.actor.user_id, tenant=tenant
        )
    creatable = context.capabilities(context.record(proposed), "create", CREATE)
    required = {f.field_id for f in fields if CREATE in f.accesses}
    meta = {
        "read": readable,
        "query": queryable,
        "create": creatable,
        "can_create": required <= set(creatable)
        and (surface != "data-scope-demo" or proposed["dept_id"] is not None),
    }
    return {
        "records": records,
        "total": total,
        "current": query.current,
        "size": query.size,
        "field_policy": meta,
    }
