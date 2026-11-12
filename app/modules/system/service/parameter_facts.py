"""Complete ordered keysets with a per-set sentinel and no hidden value fetch."""

from sqlalchemy import case, select, tuple_

from app.core.field_policy.contracts import limit
from app.modules.system.models.config import Config
from app.modules.system.service.parameter_context import value_ceiling


async def parameter_rows(
    db, table, tenant_id, *, predicates=(), names=None, expressions=None
):
    keys = tuple(table.primary_key.columns)
    selected = (
        tuple(
            table.c[name]
            for name in dict.fromkeys((*names, *(column.name for column in keys)))
        )
        if names is not None
        else tuple(table.c)
    )
    columns = tuple(
        case((value_ceiling(table), column), else_=None).label(column.name)
        if table is Config.__table__ and column.name == "config_value"
        else (expressions or {}).get(column.name, column)
        for column in selected
    )
    result = []
    last = None
    while True:
        filters = [table.c.tenant_id == tenant_id, *predicates]
        if last is not None:
            filters.append(tuple_(*keys) > last)
        batch = (
            (
                await db.execute(
                    select(*columns).where(*filters).order_by(*keys).limit(256)
                )
            )
            .mappings()
            .all()
        )
        result.extend(dict(row) for row in batch)
        limit(len(result) <= 10000)
        if len(batch) < 256:
            return result
        last = tuple(batch[-1][c.name] for c in keys)
