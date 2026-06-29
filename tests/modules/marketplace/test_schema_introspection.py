from app.modules.marketplace.lowcode.schema_introspection import (
    introspect_table,
    table_exists,
)


class TestTableExists:
    async def test_returns_false_for_nonexistent(self, db_session):
        result = await table_exists(db_session, "definitely_not_exist_table")
        assert result is False

    async def test_returns_true_for_existing(self, db_session):
        # 用 mk_app 表（Plan 1 已建）测试
        result = await table_exists(db_session, "mk_app")
        assert result is True


class TestIntrospectTable:
    async def test_returns_none_for_nonexistent(self, db_session):
        result = await introspect_table(db_session, "definitely_not_exist")
        assert result is None

    async def test_returns_columns_for_mk_app(self, db_session):
        """introspect mk_app（Plan 1 已建）拿到 id, name 等列"""
        result = await introspect_table(db_session, "mk_app")
        assert result is not None
        assert result.table_name == "mk_app"
        col_names = {c.column_name for c in result.columns}
        assert "id" in col_names
        assert "name" in col_names
        assert "slug" in col_names

    async def test_column_info_has_type(self, db_session):
        result = await introspect_table(db_session, "mk_app")
        id_col = next(c for c in result.columns if c.column_name == "id")
        assert id_col.data_type.upper() in ("BIGINT", "INTEGER")
        assert id_col.is_nullable is False  # PK

    async def test_column_info_nullable(self, db_session):
        result = await introspect_table(db_session, "mk_app")
        desc_col = next(c for c in result.columns if c.column_name == "description")
        assert desc_col.is_nullable is True
