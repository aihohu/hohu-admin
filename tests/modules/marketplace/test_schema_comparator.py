import pytest

from app.modules.marketplace.exceptions import AppInvalidManifestException
from app.modules.marketplace.lowcode.schema_comparator import (
    AddColumnOp,
    AlterColumnOp,
    compare_schemas,
)
from app.modules.marketplace.lowcode.schema_introspection import (
    ColumnInfo,
    TableExistsResult,
)
from app.modules.marketplace.lowcode.type_mapping import ColumnDef, PgType


class TestCompareSchemasEmptyTable:
    def test_all_columns_added(self):
        actual = TableExistsResult(table_name="t", columns=[])
        expected = {"name": {"type": "string"}, "age": {"type": "integer"}}
        diff = compare_schemas(actual, expected)
        # 新装：6 个系统字段（id 跳过 → 5 个）+ 2 个用户字段
        assert len(diff.add_columns) == 7
        added_names = {op.column_name for op in diff.add_columns}
        assert {"name", "age"}.issubset(added_names)
        assert {
            "tenant_id",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
        }.issubset(added_names)

    def test_adds_id_created_at_automatically(self):
        actual = TableExistsResult(table_name="t", columns=[])
        expected = {"name": {"type": "string"}}
        diff = compare_schemas(actual, expected)
        added_names = {op.column_name for op in diff.add_columns}
        assert "id" not in added_names  # id is skipped (PK handled separately)
        assert "tenant_id" in added_names
        assert "created_at" in added_names
        assert "updated_at" in added_names
        assert "created_by" in added_names
        assert "updated_by" in added_names
        assert "name" in added_names


class TestCompareSchemasExistingTable:
    def _make_actual(self, columns):
        col_infos = [
            ColumnInfo(
                column_name=c["name"],
                data_type=c.get("type", "bigint"),
                character_maximum_length=c.get("length"),
                numeric_precision=c.get("precision"),
                numeric_scale=c.get("scale"),
                is_nullable=c.get("nullable", True),
                column_default=c.get("default"),
            )
            for c in columns
        ]
        return TableExistsResult(table_name="t", columns=col_infos)

    def test_new_field_added(self):
        actual = self._make_actual(
            [{"name": "name", "type": "character varying", "length": 255}]
        )
        expected = {"name": {"type": "string"}, "email": {"type": "string"}}
        diff = compare_schemas(actual, expected)
        assert len(diff.add_columns) == 1
        assert diff.add_columns[0].column_name == "email"

    def test_varchar_widening_safe(self):
        actual = self._make_actual(
            [{"name": "name", "type": "character varying", "length": 100}]
        )
        expected = {"name": {"type": "string", "maxLength": 200}}
        diff = compare_schemas(actual, expected)
        assert len(diff.alter_columns) == 1
        assert diff.alter_columns[0].column_name == "name"

    def test_varchar_narrowing_rejected(self):
        actual = self._make_actual(
            [{"name": "name", "type": "character varying", "length": 200}]
        )
        expected = {"name": {"type": "string", "maxLength": 100}}
        with pytest.raises(AppInvalidManifestException):
            compare_schemas(actual, expected)

    def test_type_change_rejected(self):
        actual = self._make_actual(
            [{"name": "score", "type": "character varying", "length": 50}]
        )
        expected = {"score": {"type": "integer"}}
        with pytest.raises(AppInvalidManifestException):
            compare_schemas(actual, expected)

    def test_no_change_returns_empty_diff(self):
        actual = self._make_actual(
            [{"name": "name", "type": "character varying", "length": 255}]
        )
        expected = {"name": {"type": "string"}}
        diff = compare_schemas(actual, expected)
        assert len(diff.add_columns) == 0
        assert len(diff.alter_columns) == 0

    def test_dropped_field_not_in_diff(self):
        actual = self._make_actual(
            [
                {"name": "name", "type": "character varying", "length": 255},
                {"name": "legacy", "type": "text"},
            ]
        )
        expected = {"name": {"type": "string"}}
        diff = compare_schemas(actual, expected)
        all_ops = diff.add_columns + diff.alter_columns
        assert all(op.column_name != "legacy" for op in all_ops)


class TestSchemaDiffDDL:
    def test_add_column_sql(self):
        op = AddColumnOp(
            column_name="email",
            col_def=ColumnDef(pg_type=PgType.VARCHAR, length=255),
            nullable=True,
            default=None,
        )
        sql = op.to_sql(table_name="app_data_test_customer")
        assert "ADD COLUMN" in sql
        assert "email" in sql
        assert "VARCHAR(255)" in sql

    def test_add_column_with_default(self):
        op = AddColumnOp(
            column_name="level",
            col_def=ColumnDef(pg_type=PgType.VARCHAR, length=10),
            nullable=False,
            default="C",
        )
        sql = op.to_sql(table_name="t")
        assert "NOT NULL" in sql
        assert "DEFAULT 'C'" in sql

    def test_alter_varchar_length_sql(self):
        op = AlterColumnOp(
            column_name="name",
            col_def=ColumnDef(pg_type=PgType.VARCHAR, length=500),
        )
        sql = op.to_sql(table_name="t")
        assert "ALTER COLUMN name TYPE VARCHAR(500)" in sql
