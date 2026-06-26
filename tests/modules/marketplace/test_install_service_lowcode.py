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


class TestReinstallSchemaEvolution:
    """v1→v2 重装时，apply_upgrade 应通过 ALTER TABLE 加新字段，
    旧字段与已有数据保留。

    回归：之前 _create_app_tables 调 create_table，CREATE TABLE IF NOT EXISTS
    在表已存在时是 no-op，v2 manifest 新增字段会丢失。
    """

    async def test_reinstall_adds_new_column_preserves_data(self, db_session):
        table_name = "app_data_lowcode_evo"
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

        # --- v1: name + level ---
        app = App(
            tenant_id=0,
            name="演进 CRM",
            slug="lowcode_evo",
            type="lowcode",
            category="business",
            status="published",
        )
        db_session.add(app)
        await db_session.flush()

        v1_manifest = {
            "name": "演进 CRM",
            "slug": "lowcode_evo",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "maxLength": 100, "default": ""},
                    "level": {"type": "string", "default": "C"},
                },
                "required": ["name"],
            },
        }
        v1 = AppVersion(
            app_id=app.id,
            version="1.0.0",
            manifest=v1_manifest,
            file_url="/uploads/v1.zip",
            file_hash="0" * 64,
            file_size=1024,
            review_status="approved",
        )
        db_session.add(v1)
        await db_session.flush()
        app.current_version_id = v1.id
        await db_session.flush()

        # 安装 v1
        await install_service.install(
            db_session,
            InstallCreate(app_slug="lowcode_evo", version="1.0.0"),
            user_id=1,
        )
        await db_session.flush()

        # 写入一条数据
        await db_session.execute(
            text(f"INSERT INTO {table_name} (name, level) VALUES ('Alice', 'A')")
        )
        await db_session.flush()

        # --- v2: name + level + email(新) ---
        v2_manifest = {
            "name": "演进 CRM",
            "slug": "lowcode_evo",
            "version": "2.0.0",
            "type": "lowcode",
            "category": "business",
            "data_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "maxLength": 100, "default": ""},
                    "level": {"type": "string", "default": "C"},
                    "email": {"type": "string", "maxLength": 200, "default": ""},
                },
                "required": ["name"],
            },
        }
        v2 = AppVersion(
            app_id=app.id,
            version="2.0.0",
            manifest=v2_manifest,
            file_url="/uploads/v2.zip",
            file_hash="1" * 64,
            file_size=2048,
            review_status="approved",
        )
        db_session.add(v2)
        await db_session.flush()
        app.current_version_id = v2.id
        await db_session.flush()

        # 重装（显式指定 v2，避免 created_at DESC 排序歧义）
        await install_service.install(
            db_session,
            InstallCreate(app_slug="lowcode_evo", version="2.0.0"),
            user_id=1,
        )
        await db_session.flush()

        # 校验：email 字段已通过 ALTER TABLE 加进来
        result = await introspect_table(db_session, table_name)
        assert result is not None
        col_names = {c.column_name for c in result.columns}
        assert "name" in col_names
        assert "level" in col_names
        assert "email" in col_names, "v2 新增的 email 字段未通过 ALTER TABLE 添加"

        # 校验：原数据仍在
        rows = (
            await db_session.execute(
                text(f"SELECT name, level, email FROM {table_name}")
            )
        ).all()
        assert len(rows) == 1
        assert rows[0][0] == "Alice"
        assert rows[0][1] == "A"
        # email 是新增字段，旧行的 email 应为 default ''
        assert rows[0][2] == ""

        # 清理
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

    async def test_reinstall_widens_varchar_via_alter(self, db_session):
        """v1 VARCHAR(50) → v2 VARCHAR(200)：widening 应 ALTER COLUMN TYPE"""
        table_name = "app_data_lowcode_widen"
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))

        app = App(
            tenant_id=0,
            name="Widen CRM",
            slug="lowcode_widen",
            type="lowcode",
            category="business",
            status="published",
        )
        db_session.add(app)
        await db_session.flush()

        v1 = AppVersion(
            app_id=app.id,
            version="1.0.0",
            manifest={
                "name": "X",
                "slug": "lowcode_widen",
                "version": "1.0.0",
                "type": "lowcode",
                "category": "business",
                "data_schema": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "maxLength": 50, "default": ""}
                    },
                    "required": ["code"],
                },
            },
            file_url="/uploads/v1.zip",
            file_hash="0" * 64,
            file_size=1024,
            review_status="approved",
        )
        db_session.add(v1)
        await db_session.flush()
        app.current_version_id = v1.id
        await db_session.flush()

        await install_service.install(
            db_session,
            InstallCreate(app_slug="lowcode_widen", version="1.0.0"),
            user_id=1,
        )
        await db_session.flush()

        # 校验初版长度
        result = await introspect_table(db_session, table_name)
        assert result is not None
        code_col = next(c for c in result.columns if c.column_name == "code")
        assert code_col.character_maximum_length == 50

        # v2: code VARCHAR(200)
        v2 = AppVersion(
            app_id=app.id,
            version="2.0.0",
            manifest={
                "name": "X",
                "slug": "lowcode_widen",
                "version": "2.0.0",
                "type": "lowcode",
                "category": "business",
                "data_schema": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "maxLength": 200, "default": ""}
                    },
                    "required": ["code"],
                },
            },
            file_url="/uploads/v2.zip",
            file_hash="1" * 64,
            file_size=2048,
            review_status="approved",
        )
        db_session.add(v2)
        await db_session.flush()
        app.current_version_id = v2.id
        await db_session.flush()

        await install_service.install(
            db_session,
            InstallCreate(app_slug="lowcode_widen", version="2.0.0"),
            user_id=1,
        )
        await db_session.flush()

        # 校验：长度已 widening 到 200
        result = await introspect_table(db_session, table_name)
        assert result is not None
        code_col = next(c for c in result.columns if c.column_name == "code")
        assert code_col.character_maximum_length == 200

        # 清理
        await db_session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
