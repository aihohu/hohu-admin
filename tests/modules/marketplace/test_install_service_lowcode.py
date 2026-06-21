"""低代码 install/uninstall 建表/删表集成测试（spec 6.2 + 6.4）"""

import pytest
from sqlalchemy import select, text

from app.modules.marketplace.lowcode.schema_introspection import (
    introspect_table,
    table_exists,
)
from app.modules.marketplace.models import App, AppVersion, TenantApp
from app.modules.marketplace.schemas.install import InstallCreate
from app.modules.marketplace.service.install_service import install_service


@pytest.fixture
async def lowcode_app_with_schema(db_session):
    """已发布的低代码应用，manifest 含 data_schema"""
    app = App(
        tenant_id=0,
        name="低代码 CRM",
        slug="lowcode_test_crm",
        type="lowcode",
        category="business",
        status="published",
    )
    db_session.add(app)
    await db_session.flush()

    manifest = {
        "name": "低代码 CRM",
        "slug": "lowcode_test_crm",
        "version": "1.0.0",
        "type": "lowcode",
        "category": "business",
        "data_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": 100, "default": ""},
                "level": {"type": "string", "default": "C"},
            },
            "required": ["name", "level"],
        },
        "menu": {"title": "测试 CRM", "icon": "PeopleOutline"},
    }
    version = AppVersion(
        app_id=app.id,
        version="1.0.0",
        manifest=manifest,
        file_url="/uploads/x.zip",
        file_hash="0" * 64,
        file_size=1024,
        review_status="approved",
    )
    db_session.add(version)
    await db_session.flush()
    app.current_version_id = version.id
    await db_session.flush()
    return app


class TestInstallCreatesTables:
    async def test_install_creates_app_data_table(
        self, db_session, lowcode_app_with_schema
    ):
        """install 时根据 manifest.data_schema 自动建表"""
        table_name = "app_data_lowcode_test_crm"
        # 清理残留（防止上一个测试遗留）
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

        req = InstallCreate(app_slug=lowcode_app_with_schema.slug)
        await install_service.install(db_session, req, user_id=1)
        await db_session.flush()

        assert await table_exists(db_session, table_name)

    async def test_install_creates_table_with_user_columns(
        self, db_session, lowcode_app_with_schema
    ):
        """验证建表包含用户字段 + 系统字段"""
        table_name = "app_data_lowcode_test_crm"
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

        req = InstallCreate(app_slug=lowcode_app_with_schema.slug)
        await install_service.install(db_session, req, user_id=1)
        await db_session.flush()

        result = await introspect_table(db_session, table_name)
        assert result is not None
        col_names = {c.column_name for c in result.columns}
        # 用户字段
        assert "name" in col_names
        assert "level" in col_names
        # 系统字段
        assert "tenant_id" in col_names
        assert "created_at" in col_names


class TestUninstallDropsTables:
    async def test_uninstall_drops_table_and_records_retained(
        self, db_session, lowcode_app_with_schema
    ):
        """uninstall DROP 表 + 记录 retained_table_names"""
        table_name = "app_data_lowcode_test_crm"
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

        # install
        req = InstallCreate(app_slug=lowcode_app_with_schema.slug)
        await install_service.install(db_session, req, user_id=1)
        await db_session.flush()
        assert await table_exists(db_session, table_name)

        # uninstall
        await install_service.uninstall(
            db_session, app_id=lowcode_app_with_schema.id, user_id=1
        )
        await db_session.flush()

        # 表已 DROP
        assert not await table_exists(db_session, table_name)

        # tenant_app.retained_table_names 记录了曾存在的表
        result = await db_session.execute(
            select(TenantApp).where(TenantApp.app_id == lowcode_app_with_schema.id)
        )
        record = result.scalar_one()
        assert record.status == "uninstalled"
        assert table_name in (record.retained_table_names or [])
        assert record.has_data is True


class TestInstallNoDataSchema:
    async def test_install_without_data_schema_no_table(self, db_session):
        """manifest 无 data_schema → 不建表"""
        app = App(
            tenant_id=0,
            name="纯展示",
            slug="lowcode_no_schema",
            type="lowcode",
            category="business",
            status="published",
        )
        db_session.add(app)
        await db_session.flush()
        version = AppVersion(
            app_id=app.id,
            version="1.0.0",
            manifest={
                "name": "X",
                "slug": "lowcode_no_schema",
                "version": "1.0.0",
                "type": "lowcode",
                "category": "business",
            },  # 无 data_schema
            file_url="/uploads/x.zip",
            file_hash="0" * 64,
            file_size=1024,
            review_status="approved",
        )
        db_session.add(version)
        await db_session.flush()
        app.current_version_id = version.id

        req = InstallCreate(app_slug="lowcode_no_schema")
        await install_service.install(db_session, req, user_id=1)
        await db_session.flush()

        assert not await table_exists(db_session, "app_data_lowcode_no_schema")


class TestMultiModelInstall:
    async def test_install_creates_multiple_tables(self, db_session):
        """有 models 数组 → 每个 model 独立建表"""
        app = App(
            tenant_id=0,
            name="多表 CRM",
            slug="lowcode_multi_model",
            type="lowcode",
            category="business",
            status="published",
        )
        db_session.add(app)
        await db_session.flush()
        version = AppVersion(
            app_id=app.id,
            version="1.0.0",
            manifest={
                "name": "X",
                "slug": "lowcode_multi_model",
                "version": "1.0.0",
                "type": "lowcode",
                "category": "business",
                "models": [
                    {
                        "key": "customer",
                        "data_schema": {
                            "type": "object",
                            "properties": {"name": {"type": "string", "default": ""}},
                            "required": ["name"],
                        },
                    },
                    {
                        "key": "order",
                        "data_schema": {
                            "type": "object",
                            "properties": {"amount": {"type": "number", "default": 0}},
                            "required": ["amount"],
                        },
                    },
                ],
            },
            file_url="/uploads/x.zip",
            file_hash="0" * 64,
            file_size=1024,
            review_status="approved",
        )
        db_session.add(version)
        await db_session.flush()
        app.current_version_id = version.id

        # 清理残留
        await db_session.execute(
            text("DROP TABLE IF EXISTS app_data_lowcode_multi_model_customer")
        )
        await db_session.execute(
            text("DROP TABLE IF EXISTS app_data_lowcode_multi_model_order")
        )

        req = InstallCreate(app_slug="lowcode_multi_model")
        await install_service.install(db_session, req, user_id=1)
        await db_session.flush()

        assert await table_exists(db_session, "app_data_lowcode_multi_model_customer")
        assert await table_exists(db_session, "app_data_lowcode_multi_model_order")


class TestHyphenatedSlugInstall:
    async def test_install_with_hyphenated_slug_creates_table(self, db_session):
        """Slug with hyphens (production format: author-name) should work.

        Regression: 之前直接 `f"app_data_{slug}"` 会生成 `app_data_zhangsan-crm`，
        PostgreSQL 把连字符当操作符解析，CREATE TABLE 直接语法错误。
        """
        app = App(
            tenant_id=0,
            name="CRM",
            slug="zhangsan-crm",  # hyphenated!
            type="lowcode",
            category="business",
            status="published",
        )
        db_session.add(app)
        await db_session.flush()

        version = AppVersion(
            app_id=app.id,
            version="1.0.0",
            manifest={
                "name": "CRM",
                "slug": "zhangsan-crm",
                "version": "1.0.0",
                "type": "lowcode",
                "category": "business",
                "data_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "default": ""}},
                    "required": ["name"],
                },
            },
            file_url="/uploads/x.zip",
            file_hash="0" * 64,
            file_size=1024,
            review_status="approved",
        )
        db_session.add(version)
        await db_session.flush()
        app.current_version_id = version.id

        # Clean up any residue (both sanitized and un-sanitized names just in case)
        await db_session.execute(text("DROP TABLE IF EXISTS app_data_zhangsan_crm"))
        await db_session.execute(text('DROP TABLE IF EXISTS "app_data_zhangsan-crm"'))

        req = InstallCreate(app_slug="zhangsan-crm")
        await install_service.install(db_session, req, user_id=1)
        await db_session.flush()

        # Table should be created with underscore (not hyphen)
        assert await table_exists(db_session, "app_data_zhangsan_crm")
        # Table with hyphen should NOT exist
        assert not await table_exists(db_session, "app_data_zhangsan-crm")
