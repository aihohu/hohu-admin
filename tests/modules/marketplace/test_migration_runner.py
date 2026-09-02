import pytest
from sqlalchemy import text

from app.modules.marketplace.lowcode.migration_runner import MigrationRunner
from app.modules.marketplace.lowcode.schema_introspection import (
    introspect_table,
    table_exists,
)
from modules.marketplace import DEFAULT_TENANT


@pytest.fixture
def runner():
    return MigrationRunner()


@pytest.fixture
def table_name(request):
    """每个测试独立表名，避免冲突"""
    return f"app_data_test_{request.node.name}"


class TestCreateTable:
    async def test_creates_table_with_user_fields(self, db_session, runner, table_name):
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        try:
            schema = {
                "properties": {
                    "name": {"type": "string"},
                    "age": {"type": "integer"},
                },
                "required": [],
            }
            await runner.create_table(
                db_session,
                table_name=table_name,
                data_schema=schema,
                tenant=DEFAULT_TENANT,
            )

            assert await table_exists(db_session, table_name)
            result = await introspect_table(db_session, table_name)
            assert result is not None
            col_names = {c.column_name for c in result.columns}
            # 系统字段
            assert {
                "id",
                "tenant_id",
                "created_at",
                "updated_at",
                "created_by",
                "updated_by",
            }.issubset(col_names)
            tenant_column = next(
                column for column in result.columns if column.column_name == "tenant_id"
            )
            assert tenant_column.column_default is None
            # 用户字段
            assert {"name", "age"}.issubset(col_names)
        finally:
            await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

    async def test_required_field_with_default(self, db_session, runner, table_name):
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        try:
            schema = {
                "properties": {
                    "level": {"type": "string", "default": "C"},
                },
                "required": ["level"],
            }
            await runner.create_table(
                db_session,
                table_name=table_name,
                data_schema=schema,
                tenant=DEFAULT_TENANT,
            )

            result = await introspect_table(db_session, table_name)
            assert result is not None
            level_col = next(c for c in result.columns if c.column_name == "level")
            assert level_col.is_nullable is False
            assert level_col.column_default is not None
            assert "C" in level_col.column_default
        finally:
            await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))


class TestApplyUpgrade:
    async def test_add_column(self, db_session, runner, table_name):
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        try:
            # 先建初始表
            initial = {
                "properties": {"name": {"type": "string"}},
                "required": [],
            }
            await runner.create_table(
                db_session,
                table_name=table_name,
                data_schema=initial,
                tenant=DEFAULT_TENANT,
            )

            # 升级：增加 email 字段
            upgraded = {
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                },
                "required": [],
            }
            await runner.apply_upgrade(
                db_session,
                table_name=table_name,
                new_data_schema=upgraded,
                tenant=DEFAULT_TENANT,
            )

            result = await introspect_table(db_session, table_name)
            assert result is not None
            col_names = {c.column_name for c in result.columns}
            assert "email" in col_names
        finally:
            await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

    async def test_varchar_widening(self, db_session, runner, table_name):
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        try:
            # 先建 VARCHAR(100)
            initial = {
                "properties": {"name": {"type": "string", "maxLength": 100}},
                "required": [],
            }
            await runner.create_table(
                db_session,
                table_name=table_name,
                data_schema=initial,
                tenant=DEFAULT_TENANT,
            )

            # 升级到 VARCHAR(200)
            upgraded = {
                "properties": {"name": {"type": "string", "maxLength": 200}},
                "required": [],
            }
            await runner.apply_upgrade(
                db_session,
                table_name=table_name,
                new_data_schema=upgraded,
                tenant=DEFAULT_TENANT,
            )

            result = await introspect_table(db_session, table_name)
            assert result is not None
            name_col = next(c for c in result.columns if c.column_name == "name")
            assert name_col.character_maximum_length == 200
        finally:
            await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

    async def test_creates_when_not_exists(self, db_session, runner, table_name):
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        try:
            schema = {
                "properties": {"title": {"type": "string"}},
                "required": [],
            }
            await runner.apply_upgrade(
                db_session,
                table_name=table_name,
                new_data_schema=schema,
                tenant=DEFAULT_TENANT,
            )
            assert await table_exists(db_session, table_name)
        finally:
            await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))


class TestDropTable:
    async def test_drop_existing(self, db_session, runner, table_name):
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        try:
            schema = {"properties": {"x": {"type": "string"}}, "required": []}
            await runner.create_table(
                db_session,
                table_name=table_name,
                data_schema=schema,
                tenant=DEFAULT_TENANT,
            )
            assert await table_exists(db_session, table_name)

            await runner.drop_table(
                db_session, table_name=table_name, tenant=DEFAULT_TENANT
            )
            assert not await table_exists(db_session, table_name)
        finally:
            await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

    async def test_drop_nonexistent_is_noop(self, db_session, runner, table_name):
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        # 不应抛异常
        await runner.drop_table(
            db_session, table_name=table_name, tenant=DEFAULT_TENANT
        )
        assert not await table_exists(db_session, table_name)


class TestGetTableNamesForApp:
    async def test_returns_matching_tables(self, db_session, runner):
        slug = "testlistapp"
        t1 = f"app_data_{slug}_customer"
        t2 = f"app_data_{slug}_order"
        await db_session.execute(text(f"DROP TABLE IF EXISTS {t1}"))
        await db_session.execute(text(f"DROP TABLE IF EXISTS {t2}"))
        try:
            schema = {"properties": {"x": {"type": "string"}}, "required": []}
            await runner.create_table(
                db_session,
                table_name=t1,
                data_schema=schema,
                tenant=DEFAULT_TENANT,
            )
            await runner.create_table(
                db_session,
                table_name=t2,
                data_schema=schema,
                tenant=DEFAULT_TENANT,
            )

            names = await runner.get_table_names_for_app(
                db_session, app_slug=slug, tenant=DEFAULT_TENANT
            )
            assert t1 in names
            assert t2 in names
        finally:
            await db_session.execute(text(f"DROP TABLE IF EXISTS {t1}"))
            await db_session.execute(text(f"DROP TABLE IF EXISTS {t2}"))
