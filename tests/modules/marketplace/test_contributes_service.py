import pytest
from sqlalchemy import update

from app.core.redis import get_redis
from app.modules.marketplace.models import App, AppVersion, TenantApp
from app.modules.marketplace.service.contributes_service import (
    CACHE_KEY_PATTERN,
    contributes_service,
)


@pytest.fixture
async def installed_app_with_menu(db_session):
    """已启用应用，manifest 含 menu + pages"""
    app = App(
        tenant_id=0,
        name="X",
        slug="contributes_test",
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
            "slug": "contributes_test",
            "version": "1.0.0",
            "type": "lowcode",
            "category": "business",
            "menu": {
                "title": "测试 CRM",
                "icon": "PeopleOutline",
                "parent": None,
                "order": 100,
                "page_key": "list",
            },
            "pages": [
                {"key": "list", "page_type": "table", "title": "列表"},
                {"key": "form", "page_type": "form", "title": "表单"},
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
    tenant_app = TenantApp(
        tenant_id=0,
        app_id=app.id,
        installed_version="1.0.0",
        status="enabled",
    )
    db_session.add(tenant_app)
    await db_session.flush()
    return app


class TestContributesAggregator:
    async def test_aggregate_returns_menus_and_pages(
        self,
        db_session,
        installed_app_with_menu,  # noqa: ARG002
    ):
        result = await contributes_service.aggregate_for_tenant(db_session, tenant_id=0)
        assert any(m["app_slug"] == "contributes_test" for m in result["menus"])
        page_keys = {
            p["key"] for p in result["pages"] if p["app_slug"] == "contributes_test"
        }
        assert {"list", "form"}.issubset(page_keys)

    async def test_aggregate_excludes_disabled_apps(
        self, db_session, installed_app_with_menu
    ):
        await db_session.execute(
            update(TenantApp)
            .where(TenantApp.app_id == installed_app_with_menu.id)
            .values(status="disabled")
        )
        await db_session.flush()
        result = await contributes_service.aggregate_for_tenant(db_session, tenant_id=0)
        assert all(m["app_slug"] != "contributes_test" for m in result["menus"])

    async def test_aggregate_menu_has_correct_fields(
        self,
        db_session,
        installed_app_with_menu,  # noqa: ARG002
    ):
        result = await contributes_service.aggregate_for_tenant(db_session, tenant_id=0)
        menu = next(m for m in result["menus"] if m["app_slug"] == "contributes_test")
        assert menu["title"] == "测试 CRM"
        assert menu["icon"] == "PeopleOutline"
        assert menu["order"] == 100
        assert menu["page_key"] == "list"

    async def test_aggregate_menu_page_key_defaults_to_none_when_missing(
        self,
        db_session,
        installed_app_with_menu,
    ):
        """manifest 未声明 menu.page_key 时字段为 None（前端 fallback 到 first page）"""
        # 移除 page_key
        version = await db_session.get(
            AppVersion, installed_app_with_menu.current_version_id
        )
        manifest = dict(version.manifest)
        manifest["menu"] = {
            "title": "无 page_key",
            "icon": None,
            "parent": None,
            "order": 100,
        }
        version.manifest = manifest
        await db_session.flush()
        result = await contributes_service.aggregate_for_tenant(db_session, tenant_id=0)
        menu = next(m for m in result["menus"] if m["app_slug"] == "contributes_test")
        assert menu["page_key"] is None

    async def test_cache_write_and_read(
        self,
        db_session,
        installed_app_with_menu,  # noqa: ARG002
        redis_ready,  # noqa: ARG002
    ):
        # 写缓存
        await contributes_service.refresh_cache(db_session, tenant_id=0)
        cached = await contributes_service.get_cached(tenant_id=0)
        assert cached is not None
        assert any(m["app_slug"] == "contributes_test" for m in cached["menus"])

    async def test_cache_invalidate(
        self,
        db_session,
        installed_app_with_menu,  # noqa: ARG002
        redis_ready,  # noqa: ARG002
    ):
        await contributes_service.refresh_cache(db_session, tenant_id=0)
        assert await contributes_service.get_cached(tenant_id=0) is not None

        await contributes_service.invalidate(tenant_id=0)
        assert await contributes_service.get_cached(tenant_id=0) is None

    async def test_get_cached_returns_none_if_missing(
        self,
        redis_ready,  # noqa: ARG002
    ):
        # 清理可能残留的 key
        redis = await get_redis()
        await redis.delete(CACHE_KEY_PATTERN.format(tenant_id=99999))
        result = await contributes_service.get_cached(tenant_id=99999)
        assert result is None
