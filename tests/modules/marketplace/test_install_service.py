import pytest
from sqlalchemy import select

from app.modules.marketplace.exceptions import AppNotFoundException
from app.modules.marketplace.models import App, AppVersion, TenantApp
from app.modules.marketplace.schemas.install import InstallCreate, InstallQuery
from app.modules.marketplace.service.install_service import install_service


@pytest.fixture
async def published_app(db_session):
    """已发布应用（含一个 approved 版本）"""
    app = App(
        tenant_id=0,
        name="发布测试应用",
        slug="install-test-app",
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
            "slug": "install-test-app",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
        },
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


class TestInstallService:
    async def test_install_new_app_creates_tenant_app(self, db_session, published_app):
        """新装：INSERT tenant_app"""
        req = InstallCreate(app_slug=published_app.slug)
        result = await install_service.install(db_session, req, user_id=1)
        await db_session.flush()
        assert result.status == "installed"
        assert result.installed_version == "1.0.0"
        assert result.tenant_id == 0

    async def test_install_writes_approved_permissions(self, db_session, published_app):
        req = InstallCreate(
            app_slug=published_app.slug,
            approved_permissions=[{"type": "api", "detail": {"method": "GET"}}],
        )
        result = await install_service.install(db_session, req, user_id=1)
        await db_session.flush()
        assert result.approved_permissions == [
            {"type": "api", "detail": {"method": "GET"}}
        ]

    async def test_uninstall_then_reinstall_updates_same_row(
        self, db_session, published_app
    ):
        """卸载后重装：UPDATE 同一行"""
        req = InstallCreate(app_slug=published_app.slug)
        first = await install_service.install(db_session, req, user_id=1)
        await db_session.flush()
        first_id = first.id

        await install_service.uninstall(db_session, app_id=published_app.id, user_id=1)
        await db_session.flush()

        second = await install_service.install(db_session, req, user_id=1)
        await db_session.flush()
        second_id = second.id

        assert first_id == second_id  # 同一行（UPDATE）
        assert second.status == "installed"  # 状态回到 installed

    async def test_uninstall_sets_status_and_clears_data(
        self, db_session, published_app
    ):
        req = InstallCreate(app_slug=published_app.slug)
        await install_service.install(db_session, req, user_id=1)
        await db_session.flush()

        await install_service.uninstall(db_session, app_id=published_app.id, user_id=1)
        await db_session.flush()

        record = (
            await db_session.execute(
                select(TenantApp).where(TenantApp.app_id == published_app.id)
            )
        ).scalar_one()
        assert record.status == "uninstalled"
        # Phase 1 没建表，retained_table_names 为空 list
        assert record.retained_table_names == []
        assert record.has_data is False

    async def test_uninstall_nonexistent_raises(self, db_session):
        with pytest.raises(AppNotFoundException):
            await install_service.uninstall(db_session, app_id=99999, user_id=1)

    async def test_enable_disable(self, db_session, published_app):
        req = InstallCreate(app_slug=published_app.slug)
        await install_service.install(db_session, req, user_id=1)
        await db_session.flush()

        enabled = await install_service.enable(db_session, app_id=published_app.id)
        assert enabled.status == "enabled"

        disabled = await install_service.disable(db_session, app_id=published_app.id)
        assert disabled.status == "disabled"

    async def test_list_installed_filters_by_status(self, db_session, published_app):
        await install_service.install(
            db_session, InstallCreate(app_slug=published_app.slug), user_id=1
        )
        await db_session.flush()
        await install_service.enable(db_session, app_id=published_app.id)
        await db_session.flush()

        all_records = await install_service.list_installed(db_session, InstallQuery())
        assert len(all_records) == 1

        enabled_only = await install_service.list_installed(
            db_session, InstallQuery(status="enabled")
        )
        assert len(enabled_only) == 1
        assert enabled_only[0].status == "enabled"
