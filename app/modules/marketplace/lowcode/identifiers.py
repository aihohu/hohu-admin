"""Lowcode SQL identifier and literal boundary.

Manifest-controlled names are never accepted as generic SQL fragments.  They
must be lowercase ASCII identifiers, fit PostgreSQL's byte budget, and are
still quoted before use.
"""

import math
import re

from app.modules.marketplace.exceptions import AppInvalidManifestException

POSTGRES_IDENTIFIER_MAX_BYTES = 63
# Two indexes are named ``ix_<table>_tenant_id`` / ``..._created_at``.  Keeping
# the table at 48 bytes makes the longest generated index name 62 bytes.
LOWCODE_TABLE_MAX_BYTES = 48

SYSTEM_FIELDS = frozenset(
    {
        "id",
        "tenant_id",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
    }
)

_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_SLUG_PATTERN = re.compile(r"^[a-z](?:[a-z0-9-]*[a-z0-9])?$")


def validate_manifest_identifier(
    value: object,
    *,
    label: str,
    reject_system: bool = False,
) -> str:
    """Validate a logical model/field/relation identifier."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise AppInvalidManifestException(
            f"{label} 必须是 lower ASCII snake_case 标识符"
        )
    if len(value.encode("ascii")) > POSTGRES_IDENTIFIER_MAX_BYTES:
        raise AppInvalidManifestException(
            f"{label} 超过 PostgreSQL {POSTGRES_IDENTIFIER_MAX_BYTES} byte 限制"
        )
    if reject_system and value in SYSTEM_FIELDS:
        raise AppInvalidManifestException(f"{label} 不能覆盖系统字段：{value}")
    return value


def validate_slug(value: object) -> str:
    """Validate the non-normalized app slug accepted by physical name mapping."""
    if not isinstance(value, str) or not _SLUG_PATTERN.fullmatch(value):
        raise AppInvalidManifestException(
            "slug 只能包含小写 ASCII 字母、数字和非首尾连字符"
        )
    return value


def validate_table_name(value: object) -> str:
    """Validate a generated lowcode table and its derived-index budget."""
    name = validate_manifest_identifier(value, label="物理表名")
    if not name.startswith("app_data_"):
        raise AppInvalidManifestException("低代码物理表名必须以 app_data_ 开头")
    if len(name.encode("ascii")) > LOWCODE_TABLE_MAX_BYTES:
        raise AppInvalidManifestException(
            f"物理表名超过 {LOWCODE_TABLE_MAX_BYTES} byte 安全预算"
        )
    return name


def quote_identifier(
    value: object,
    *,
    label: str,
    reject_system: bool = False,
) -> str:
    """Validate and quote one SQL identifier for PostgreSQL."""
    name = validate_manifest_identifier(value, label=label, reject_system=reject_system)
    return f'"{name}"'


def quote_table_name(value: object) -> str:
    """Validate and quote a generated lowcode table name."""
    return f'"{validate_table_name(value)}"'


def make_index_name(table_name: object, column_name: object) -> str:
    """Build a deterministic index name without PostgreSQL truncation."""
    table = validate_table_name(table_name)
    column = validate_manifest_identifier(column_name, label="索引列")
    name = f"ix_{table}_{column}"
    if len(name.encode("ascii")) > POSTGRES_IDENTIFIER_MAX_BYTES:
        raise AppInvalidManifestException("索引名超过 PostgreSQL 63 byte 限制")
    return name


def render_sql_literal(value: object) -> str:
    """Render a restricted manifest default as a PostgreSQL literal."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AppInvalidManifestException("default 数值必须是有限值")
        return repr(value)
    if isinstance(value, str):
        if "\x00" in value:
            raise AppInvalidManifestException("default 字符串不能包含 NUL")
        escaped = value.replace("\\", "\\\\").replace("'", "''")
        return "E'" + escaped + "'"
    raise AppInvalidManifestException("default 必须是 string/number/boolean 字面常量")
