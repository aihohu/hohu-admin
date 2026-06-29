import pytest

from app.modules.marketplace.exceptions import AppInvalidManifestException
from app.modules.marketplace.lowcode.type_mapping import (
    ColumnDef,
    PgType,
    json_schema_to_pg_type,
    pg_type_to_sql,
)


class TestJsonSchemaToPgType:
    def test_string_maps_to_varchar(self):
        col_def = json_schema_to_pg_type({"type": "string"})
        assert col_def.pg_type == PgType.VARCHAR
        assert col_def.length == 255

    def test_string_with_max_length(self):
        col_def = json_schema_to_pg_type({"type": "string", "maxLength": 500})
        assert col_def.pg_type == PgType.VARCHAR
        assert col_def.length == 500

    def test_string_long_maps_to_text(self):
        """maxLength > 1000 → TEXT"""
        col_def = json_schema_to_pg_type({"type": "string", "maxLength": 5000})
        assert col_def.pg_type == PgType.TEXT

    def test_integer(self):
        col_def = json_schema_to_pg_type({"type": "integer"})
        assert col_def.pg_type == PgType.INTEGER

    def test_number(self):
        col_def = json_schema_to_pg_type({"type": "number"})
        assert col_def.pg_type == PgType.NUMERIC
        assert col_def.precision == 10
        assert col_def.scale == 2

    def test_boolean(self):
        col_def = json_schema_to_pg_type({"type": "boolean"})
        assert col_def.pg_type == PgType.BOOLEAN

    def test_string_date_format(self):
        col_def = json_schema_to_pg_type({"type": "string", "format": "date"})
        assert col_def.pg_type == PgType.DATE

    def test_string_datetime_format(self):
        col_def = json_schema_to_pg_type({"type": "string", "format": "date-time"})
        assert col_def.pg_type == PgType.TIMESTAMPTZ

    def test_array_maps_to_jsonb(self):
        col_def = json_schema_to_pg_type({"type": "array"})
        assert col_def.pg_type == PgType.JSONB

    def test_object_maps_to_jsonb(self):
        col_def = json_schema_to_pg_type({"type": "object"})
        assert col_def.pg_type == PgType.JSONB

    def test_unknown_type_raises(self):
        with pytest.raises(AppInvalidManifestException):
            json_schema_to_pg_type({"type": "invalid"})

    def test_pg_type_to_sql(self):
        col = ColumnDef(pg_type=PgType.VARCHAR, length=100)
        assert pg_type_to_sql(col) == "VARCHAR(100)"

        col = ColumnDef(pg_type=PgType.TEXT)
        assert pg_type_to_sql(col) == "TEXT"

        col = ColumnDef(pg_type=PgType.INTEGER)
        assert pg_type_to_sql(col) == "INTEGER"

        col = ColumnDef(pg_type=PgType.NUMERIC, precision=10, scale=2)
        assert pg_type_to_sql(col) == "NUMERIC(10,2)"
