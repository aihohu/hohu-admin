"""SQL closed projections; credential bytes and unknown JSON never leave PG."""

from types import SimpleNamespace

from sqlalchemy import String, and_, case, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONB

from app.core.field_policy.contracts import limit
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.provider import AiProvider


def _config_columns(table, kind):
    raw = cast(table.c.config, JSONB)
    empty = literal({}, JSONB)
    json_null = literal(None, JSONB)
    null = or_(table.c.config.is_(None), raw == json_null)
    if kind == "provider":
        supported = or_(null, raw == empty)
        value = case((null, None), else_=empty)
    else:
        generation = raw["generation"]
        numeric = []
        for name in ("max_tokens", "temperature"):
            atom = generation[name]
            numeric.append(
                or_(
                    ~generation.has_key(name),
                    atom == json_null,
                    and_(
                        func.pg_catalog.jsonb_typeof(atom) == "number",
                        func.pg_catalog.octet_length(cast(atom, String)) <= 32,
                    ),
                )
            )
        supported = or_(
            null,
            raw == empty,
            and_(
                func.pg_catalog.jsonb_typeof(raw) == "object",
                raw.op("-")("generation") == empty,
                or_(
                    generation == json_null,
                    and_(
                        func.pg_catalog.jsonb_typeof(generation) == "object",
                        generation.op("-")("max_tokens").op("-")("temperature")
                        == empty,
                        *numeric,
                    ),
                ),
            ),
        )
        value = case((supported, raw), else_=empty)
    return value.label("config"), supported.label("configuration_supported")


def provider_columns():
    table = AiProvider.__table__
    return (
        *tuple(
            table.c[name]
            for name in (
                "provider_id",
                "provider_code",
                "name",
                "base_url",
                "is_enabled",
                "create_time",
            )
        ),
        and_(table.c.api_key.is_not(None), table.c.api_key != "").label(
            "credential_configured"
        ),
        *_config_columns(table, "provider"),
    )


def model_columns():
    table = AiModel.__table__
    return (
        *tuple(table.c[name] for name in table.c.keys() if name != "config"),
        *_config_columns(table, "model"),
    )


async def providers(db, *, predicates=(), offset=0, maximum=100):
    rows = (
        (
            await db.execute(
                select(*provider_columns())
                .where(*predicates)
                .order_by(AiProvider.provider_id)
                .offset(offset)
                .limit(maximum)
            )
        )
        .mappings()
        .all()
    )
    return [SimpleNamespace(**row) for row in rows]


async def model_pairs(db, *, predicates=()):
    pc, mc = provider_columns(), model_columns()
    result, last = [], 0
    while True:
        rows = (
            (
                await db.execute(
                    select(
                        *(column.label("p_" + column.name) for column in pc),
                        *(column.label("m_" + column.name) for column in mc),
                    )
                    .select_from(AiModel)
                    .join(AiProvider, AiModel.provider_id == AiProvider.provider_id)
                    .where(AiModel.model_id > last, *predicates)
                    .order_by(AiModel.model_id)
                    .limit(256)
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            result.append(
                (
                    SimpleNamespace(
                        **{column.name: row["m_" + column.name] for column in mc}
                    ),
                    SimpleNamespace(
                        **{column.name: row["p_" + column.name] for column in pc}
                    ),
                )
            )
        limit(len(result) <= 10000)
        if len(rows) < 256:
            return result
        last = rows[-1]["m_model_id"]
