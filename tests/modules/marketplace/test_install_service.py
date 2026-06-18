import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

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

        all_page = await install_service.list_installed(db_session, InstallQuery())
        assert all_page.total == 1
        assert len(all_page.records) == 1

        enabled_page = await install_service.list_installed(
            db_session, InstallQuery(status="enabled")
        )
        assert enabled_page.total == 1
        assert len(enabled_page.records) == 1
        assert enabled_page.records[0].status == "enabled"

    async def test_list_installed_respects_pagination(self, db_session, published_app):
        """分页：size=10 默认，超过则只返回 size 条"""
        # 只有一条记录，size=1 后只返回 1 条但 total 仍是 1
        await install_service.install(
            db_session, InstallCreate(app_slug=published_app.slug), user_id=1
        )
        await db_session.flush()
        page = await install_service.list_installed(
            db_session, InstallQuery(current=1, size=1)
        )
        assert page.total == 1
        assert page.current == 1
        assert page.size == 1
        assert len(page.records) == 1

    async def test_concurrent_install_retries_as_update(
        self, db_session, published_app, monkeypatch
    ):
        """模拟 DB UNIQUE 冲突：预检返回 None（模拟并发场景），
        第一次 flush（INSERT 路径）抛 IntegrityError，rollback 后
        重新查询拿到 existing 行退化为 _do_reinstall UPDATE"""
        # 准备一个「已存在」的行（供 rollback 后重查时返回）
        # 注意：不通过 db.add 入库，因为 install 内部会 rollback 整个事务；
        # 直接通过 patched_execute 注入即可
        existing_row = TenantApp(
            id=999888,
            tenant_id=0,
            app_id=published_app.id,
            installed_version="0.9.0",
            status="uninstalled",
        )

        # install() 内部 execute 顺序：
        # 1. app_service.get_by_slug（app 查询）
        # 2. version_service.get_latest_approved（version 查询）
        # 3. install 内部预检 stmt（应返回 None 模拟并发）
        # 4. install rollback 后的重新查询 stmt（应返回 existing_row）
        original_execute = db_session.execute
        exec_call = [0]

        async def patched_execute(stmt):
            exec_call[0] += 1
            # 第 3 次：install 预检 → 返回 None（模拟并发通过了预检）
            if exec_call[0] == 3:

                class MockEmptyResult:
                    def scalar_one_or_none(self):
                        return None

                return MockEmptyResult()
            # 第 4 次：rollback 后的重查 → 返回 existing_row
            if exec_call[0] == 4:

                class MockExistingResult:
                    def scalar_one_or_none(self):
                        return existing_row

                return MockExistingResult()
            return await original_execute(stmt)

        monkeypatch.setattr(db_session, "execute", patched_execute)

        # rollback 必须存在（不抛错），但不真正回滚预先生成的对象引用
        async def patched_rollback():
            pass

        monkeypatch.setattr(db_session, "rollback", patched_rollback)

        # 第一次 flush（INSERT 路径）抛 IntegrityError；第二次（UPDATE 路径）走原始
        original_flush = db_session.flush
        flush_call = [0]

        async def patched_flush():
            flush_call[0] += 1
            if flush_call[0] == 1:
                raise IntegrityError(
                    "simulated",
                    {},
                    Exception(
                        "duplicate key value violates unique constraint "
                        '"uq_mk_tenant_app_tenant_app"'
                    ),
                )
            return await original_flush()

        monkeypatch.setattr(db_session, "flush", patched_flush)

        req = InstallCreate(app_slug=published_app.slug)
        result = await install_service.install(db_session, req, user_id=1)

        # 退化为 UPDATE：返回的就是 mock 注入的 existing_row
        assert result is existing_row
        assert result.status == "installed"
        assert result.installed_version == "1.0.0"
