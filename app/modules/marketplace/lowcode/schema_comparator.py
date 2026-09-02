"""Schema 比对：当前物理表 vs manifest 期望，生成 ALTER 补丁（spec 6.4.1 + 6.3）"""

from dataclasses import dataclass, field

from app.modules.marketplace.exceptions import AppInvalidManifestException
from app.modules.marketplace.lowcode.schema_introspection import (
    ColumnInfo,
    TableExistsResult,
)
from app.modules.marketplace.lowcode.type_mapping import (
    ColumnDef,
    PgType,
    json_schema_to_pg_type,
    pg_type_to_sql,
)


@dataclass
class AddColumnOp:
    """ADD COLUMN 操作"""

    column_name: str
    col_def: ColumnDef
    nullable: bool
    default: object | None

    def to_sql(self, table_name: str) -> str:
        type_sql = pg_type_to_sql(self.col_def)
        parts = [f"ALTER TABLE {table_name} ADD COLUMN {self.column_name} {type_sql}"]
        if not self.nullable:
            parts.append("NOT NULL")
        if self.default is not None:
            if isinstance(self.default, str):
                parts.append(f"DEFAULT '{self.default}'")
            elif isinstance(self.default, bool):
                parts.append(f"DEFAULT {str(self.default).upper()}")
            else:
                parts.append(f"DEFAULT {self.default}")
        return " ".join(parts)


@dataclass
class AlterColumnOp:
    """ALTER COLUMN 操作（仅允许 widening，禁止 narrowing / 破坏性变更）"""

    column_name: str
    col_def: ColumnDef
    new_length: int | None = None

    def to_sql(self, table_name: str) -> str:
        type_sql = pg_type_to_sql(self.col_def)
        return (
            f"ALTER TABLE {table_name} ALTER COLUMN {self.column_name} TYPE {type_sql}"
        )


@dataclass
class SchemaDiff:
    """比对结果：需要 ADD 和 ALTER 的列"""

    add_columns: list[AddColumnOp] = field(default_factory=list)
    alter_columns: list[AlterColumnOp] = field(default_factory=list)


# 所有 app_data_* 表的固定系统字段：(col_def, nullable, default)
_SYSTEM_COLUMNS: dict[str, tuple[ColumnDef, bool, object]] = {
    "id": (ColumnDef(pg_type=PgType.INTEGER), False, None),
    "tenant_id": (ColumnDef(pg_type=PgType.INTEGER), False, None),
    "created_at": (ColumnDef(pg_type=PgType.TIMESTAMPTZ), False, None),
    "updated_at": (ColumnDef(pg_type=PgType.TIMESTAMPTZ), False, None),
    "created_by": (ColumnDef(pg_type=PgType.INTEGER), True, None),
    "updated_by": (ColumnDef(pg_type=PgType.INTEGER), True, None),
}


def compare_schemas(actual: TableExistsResult | None, expected: dict) -> SchemaDiff:
    """比对期望 schema 与实际物理表，生成 ADD/ALTER 补丁。

    - actual=None 或空 columns（新装）→ 全部字段 ADD
    - actual 存在 → 逐字段比对，widening 安全；narrowing / 类型变更抛
      AppInvalidManifestException
    - expected 中不存在但 actual 有的字段（已废弃）→ 跳过保留
    """
    diff = SchemaDiff()

    expected_columns: dict[str, tuple[ColumnDef, bool, object]] = dict(_SYSTEM_COLUMNS)
    for field_name, field_def in expected.items():
        col_def = json_schema_to_pg_type(field_def)
        nullable = True
        default = field_def.get("default")
        expected_columns[field_name] = (col_def, nullable, default)

    # 新装（actual is None 或空 columns）→ 全部 ADD（跳过 id）
    if actual is None or not actual.columns:
        for name, (col_def, nullable, default) in expected_columns.items():
            if name == "id":
                continue
            diff.add_columns.append(
                AddColumnOp(
                    column_name=name,
                    col_def=col_def,
                    nullable=nullable,
                    default=default,
                )
            )
        return diff

    # 已有表 → 仅比对用户字段（系统字段在 CREATE TABLE 时已建好）
    actual_by_name = {c.column_name: c for c in actual.columns}
    for field_name, field_def in expected.items():
        col_def = json_schema_to_pg_type(field_def)
        nullable = True
        default = field_def.get("default")

        if field_name not in actual_by_name:
            diff.add_columns.append(
                AddColumnOp(
                    column_name=field_name,
                    col_def=col_def,
                    nullable=nullable,
                    default=default,
                )
            )
            continue

        _check_compatible(actual_by_name[field_name], col_def, field_name, diff)

    return diff


def _check_compatible(
    actual: ColumnInfo,
    expected: ColumnDef,
    field_name: str,
    diff: SchemaDiff,
) -> None:
    """检查单个字段是否兼容；widening 写入 diff，破坏性变更抛异常。"""
    # VARCHAR widening / narrowing
    if expected.pg_type == PgType.VARCHAR and actual.data_type.lower() in (
        "character varying",
        "varchar",
    ):
        actual_len = actual.character_maximum_length or 255
        expected_len = expected.length or 255
        if expected_len > actual_len:
            diff.alter_columns.append(
                AlterColumnOp(
                    column_name=field_name,
                    col_def=expected,
                    new_length=expected_len,
                )
            )
        elif expected_len < actual_len:
            raise AppInvalidManifestException(
                f"字段 '{field_name}' VARCHAR 缩短（{actual_len} → {expected_len}）"
                f"是破坏性变更，拒绝自动迁移"
            )
        return

    # VARCHAR → TEXT（widening，安全）
    if expected.pg_type == PgType.TEXT and actual.data_type.lower() in (
        "character varying",
        "varchar",
    ):
        diff.alter_columns.append(
            AlterColumnOp(column_name=field_name, col_def=expected)
        )
        return

    # 整体类型检查
    actual_pg = _infer_pg_type(actual)
    if actual_pg != expected.pg_type:
        # INTEGER → NUMERIC（widening，安全）
        if actual_pg == PgType.INTEGER and expected.pg_type == PgType.NUMERIC:
            diff.alter_columns.append(
                AlterColumnOp(column_name=field_name, col_def=expected)
            )
            return
        raise AppInvalidManifestException(
            f"字段 '{field_name}' 类型变更（{actual_pg.value} → "
            f"{expected.pg_type.value}）是破坏性变更，拒绝自动迁移"
        )


def _infer_pg_type(col: ColumnInfo) -> PgType:
    """information_schema 的 data_type → PgType"""
    dt = col.data_type.lower()
    if dt in ("character varying", "varchar"):
        return PgType.VARCHAR
    if dt == "text":
        return PgType.TEXT
    if dt in ("integer", "int", "int4"):
        return PgType.INTEGER
    if dt in ("bigint", "int8"):
        return PgType.INTEGER
    if dt == "numeric":
        return PgType.NUMERIC
    if dt == "boolean":
        return PgType.BOOLEAN
    if dt == "date":
        return PgType.DATE
    if dt in ("timestamp with time zone", "timestamptz"):
        return PgType.TIMESTAMPTZ
    if dt == "jsonb":
        return PgType.JSONB
    return PgType.TEXT
