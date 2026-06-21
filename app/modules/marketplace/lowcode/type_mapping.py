# app/modules/marketplace/lowcode/type_mapping.py
"""JSON Schema → PostgreSQL 类型映射（spec 6.2）

string              → VARCHAR(255) / TEXT（maxLength > 1000）
integer             → INTEGER
number              → NUMERIC(10,2)
boolean             → BOOLEAN
string(format:date) → DATE
string(format:date-time) → TIMESTAMPTZ
array / object      → JSONB
"""

from dataclasses import dataclass
from enum import StrEnum

from app.modules.marketplace.exceptions import AppInvalidManifestException


class PgType(StrEnum):
    VARCHAR = "VARCHAR"
    TEXT = "TEXT"
    INTEGER = "INTEGER"
    NUMERIC = "NUMERIC"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    TIMESTAMPTZ = "TIMESTAMPTZ"
    JSONB = "JSONB"


@dataclass
class ColumnDef:
    """PG 列定义"""

    pg_type: PgType
    length: int | None = None
    precision: int | None = None
    scale: int | None = None


DEFAULT_VARCHAR_LENGTH = 255
TEXT_THRESHOLD = 1000
DEFAULT_NUMERIC_PRECISION = 10
DEFAULT_NUMERIC_SCALE = 2


def json_schema_to_pg_type(field_def: dict) -> ColumnDef:
    """JSON Schema field 定义 → PG 列定义"""
    json_type = field_def.get("type")
    if json_type is None:
        raise AppInvalidManifestException("字段定义缺少 type")

    if json_type == "string":
        fmt = field_def.get("format")
        if fmt == "date":
            return ColumnDef(pg_type=PgType.DATE)
        if fmt in ("date-time", "datetime"):
            return ColumnDef(pg_type=PgType.TIMESTAMPTZ)
        max_length = field_def.get("maxLength")
        if max_length is None:
            return ColumnDef(pg_type=PgType.VARCHAR, length=DEFAULT_VARCHAR_LENGTH)
        if max_length > TEXT_THRESHOLD:
            return ColumnDef(pg_type=PgType.TEXT)
        return ColumnDef(pg_type=PgType.VARCHAR, length=max_length)

    if json_type == "integer":
        return ColumnDef(pg_type=PgType.INTEGER)

    if json_type == "number":
        return ColumnDef(
            pg_type=PgType.NUMERIC,
            precision=DEFAULT_NUMERIC_PRECISION,
            scale=DEFAULT_NUMERIC_SCALE,
        )

    if json_type == "boolean":
        return ColumnDef(pg_type=PgType.BOOLEAN)

    if json_type in ("array", "object"):
        return ColumnDef(pg_type=PgType.JSONB)

    raise AppInvalidManifestException(f"不支持的 JSON Schema type: {json_type}")


def pg_type_to_sql(col: ColumnDef) -> str:
    """ColumnDef → SQL DDL 字符串"""
    if col.pg_type == PgType.VARCHAR:
        return f"VARCHAR({col.length or DEFAULT_VARCHAR_LENGTH})"
    if col.pg_type == PgType.NUMERIC:
        p = col.precision or DEFAULT_NUMERIC_PRECISION
        s = col.scale or DEFAULT_NUMERIC_SCALE
        return f"NUMERIC({p},{s})"
    return col.pg_type.value


def slug_to_table_prefix(slug: str) -> str:
    """Convert app slug to safe table name prefix.

    Replaces hyphens and dots (treated as operators in PG unquoted identifiers)
    with underscores.

    Example: 'zhangsan-crm' → 'zhangsan_crm'
    """
    return slug.replace("-", "_").replace(".", "_")


def make_table_name(slug: str, model_key: str | None = None) -> str:
    """Generate app_data_* table name from slug + optional model key.

    Examples:
        make_table_name('zhangsan-crm') → 'app_data_zhangsan_crm'
        make_table_name('zhangsan-crm', 'customer') → 'app_data_zhangsan_crm_customer'
    """
    prefix = slug_to_table_prefix(slug)
    if model_key:
        return f"app_data_{prefix}_{model_key}"
    return f"app_data_{prefix}"
