from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import func, select

from app.core.config import settings
from app.core.exceptions import AuthorizationException, NotFoundException
from app.core.tenant import TenantContext
from app.db.session import get_db
from app.main import app
from app.modules.auth.service import get_current_tenant_context
from app.modules.marketplace.capability import (
    MARKETPLACE_HOSTED_UNAVAILABLE,
    require_marketplace_capability,
    require_marketplace_http_capability,
)
from app.modules.marketplace.exceptions import AppNotFoundException
from app.modules.marketplace.lowcode.data_api_service import data_api_service
from app.modules.marketplace.lowcode.migration_runner import MigrationRunner
from app.modules.marketplace.models import (
    App,
    AppPermission,
    AppRating,
    AppReview,
    AppVersion,
    TenantApp,
)
from app.modules.marketplace.schemas.app import AppQuery
from app.modules.marketplace.schemas.install import InstallCreate, InstallQuery
from app.modules.marketplace.schemas.rating import RatingCreate
from app.modules.marketplace.service.app_service import app_service
from app.modules.marketplace.service.contributes_service import contributes_service
from app.modules.marketplace.service.install_service import install_service
from app.modules.marketplace.service.permission_service import permission_service
from app.modules.marketplace.service.rating_service import rating_service
from app.modules.marketplace.service.review_service import review_service
from app.modules.marketplace.service.upload_service import upload_service
from app.modules.marketplace.service.version_service import version_service

DEFAULT_TENANT = TenantContext(
    tenant_id=0,
    tenant_code="default",
    actor_user_id=1,
    tenant_version=1,
    source="access_token",
)
TENANT_B = TenantContext(
    tenant_id=200,
    tenant_code="tenant-b",
    actor_user_id=201,
    tenant_version=1,
    source="access_token",
)


def test_capability_allows_only_single_mode_default_tenant(monkeypatch):
    monkeypatch.setattr(settings, "TENANT_MODE", "single")
    require_marketplace_capability(DEFAULT_TENANT)

    with pytest.raises(AuthorizationException) as exc_info:
        require_marketplace_capability(TENANT_B)
    assert exc_info.value.error_code == MARKETPLACE_HOSTED_UNAVAILABLE

    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    with pytest.raises(AuthorizationException) as exc_info:
        require_marketplace_capability(DEFAULT_TENANT)
    assert exc_info.value.error_code == MARKETPLACE_HOSTED_UNAVAILABLE


def test_marketplace_models_have_no_implicit_tenant_default():
    for model in (App, TenantApp):
        tenant_column = model.__table__.c.tenant_id
        assert tenant_column.default is None
        assert tenant_column.server_default is None


def test_every_marketplace_http_route_runs_the_containment_guard_first():
    prefixes = ("/marketplace", "/api/v1/contributes", "/api/v1/app-data")
    protected_routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith(prefixes)
    ]

    assert protected_routes
    for route in protected_routes:
        assert route.dependant.dependencies
        assert (
            route.dependant.dependencies[0].call is require_marketplace_http_capability
        ), route.path


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/marketplace/list", {}),
        ("get", "/marketplace/detail/known-app", {}),
        ("post", "/marketplace/install", {"json": {"appSlug": "known-app"}}),
        ("get", "/marketplace/admin/reviews", {}),
        (
            "post",
            "/marketplace/developer/upload",
            {
                "data": {"manifest_json": "{}"},
                "files": {"file": ("app.zip", b"content", "application/zip")},
            },
        ),
        ("get", "/api/v1/contributes/", {}),
        ("post", "/api/v1/app-data/demo/_", {"json": {"name": "blocked"}}),
    ],
)
async def test_tenant_b_endpoints_fail_before_business_side_effects(
    client, monkeypatch, method, path, kwargs
):
    monkeypatch.setattr(settings, "TENANT_MODE", "single")
    app.dependency_overrides[get_current_tenant_context] = lambda: TENANT_B
    db_calls = 0

    async def forbidden_db():
        nonlocal db_calls
        db_calls += 1
        yield AsyncMock()

    app.dependency_overrides[get_db] = forbidden_db
    business_calls = [
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    ]
    try:
        with (
            patch.object(app_service, "list", business_calls[0]),
            patch.object(app_service, "get_by_slug", business_calls[1]),
            patch.object(install_service, "install", business_calls[2]),
            patch(
                "app.modules.marketplace.api.admin.review_service.list_reviews",
                business_calls[3],
            ),
            patch(
                "app.modules.marketplace.api.developer.developer_service.submit_version",
                business_calls[4],
            ),
            patch.object(contributes_service, "get_cached", business_calls[5]),
        ):
            response = await getattr(client, method)(path, **kwargs)
    finally:
        app.dependency_overrides.pop(get_current_tenant_context, None)
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 403
    assert response.json()["errorCode"] == MARKETPLACE_HOSTED_UNAVAILABLE
    assert db_calls == 0
    assert all(call.await_count == 0 for call in business_calls)


async def test_hosted_default_tenant_endpoint_is_unreachable_without_db(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    app.dependency_overrides[get_current_tenant_context] = lambda: DEFAULT_TENANT
    forbidden_db = AsyncMock()
    app.dependency_overrides[get_db] = forbidden_db
    business_call = AsyncMock()
    try:
        with patch.object(app_service, "list", business_call):
            response = await client.get("/marketplace/list")
    finally:
        app.dependency_overrides.pop(get_current_tenant_context, None)
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 403
    assert response.json()["errorCode"] == MARKETPLACE_HOSTED_UNAVAILABLE
    forbidden_db.assert_not_awaited()
    business_call.assert_not_awaited()


async def test_service_guards_reject_tenant_b_before_db_redis_or_ddl(monkeypatch):
    monkeypatch.setattr(settings, "TENANT_MODE", "single")
    db = AsyncMock()
    redis_factory = AsyncMock()
    apply_upgrade = AsyncMock()
    upload = Mock()
    upload.read = Mock()
    migration_runner = MigrationRunner()

    with (
        patch(
            "app.modules.marketplace.service.contributes_service.get_redis",
            redis_factory,
        ),
        patch.object(
            install_service.migration_runner,
            "apply_upgrade",
            apply_upgrade,
        ),
    ):
        calls = (
            app_service.list(db, AppQuery(), tenant=TENANT_B),
            install_service.install(
                db,
                InstallCreate(app_slug="known-app"),
                user_id=TENANT_B.actor_user_id,
                tenant=TENANT_B,
            ),
            install_service.list_installed(
                db,
                InstallQuery(),
                tenant=TENANT_B,
            ),
            contributes_service.get_cached(tenant=TENANT_B),
            data_api_service.create(
                db,
                table_name="app_data_demo",
                data={"name": "blocked"},
                user_id=TENANT_B.actor_user_id,
                tenant=TENANT_B,
            ),
            upload_service.save(
                file_obj=upload,
                filename="blocked.zip",
                slug="blocked",
                version="1.0.0",
                tenant=TENANT_B,
            ),
            migration_runner.create_table(
                db,
                table_name="app_data_blocked",
                data_schema={"properties": {}},
                tenant=TENANT_B,
            ),
            migration_runner.apply_upgrade(
                db,
                table_name="app_data_blocked",
                new_data_schema={"properties": {}},
                tenant=TENANT_B,
            ),
            migration_runner.drop_table(
                db,
                table_name="app_data_blocked",
                tenant=TENANT_B,
            ),
            migration_runner.get_table_names_for_app(
                db,
                app_slug="blocked",
                tenant=TENANT_B,
            ),
        )
        for call in calls:
            with pytest.raises(AuthorizationException) as exc_info:
                await call
            assert exc_info.value.error_code == MARKETPLACE_HOSTED_UNAVAILABLE

    db.execute.assert_not_awaited()
    db.flush.assert_not_awaited()
    redis_factory.assert_not_awaited()
    apply_upgrade.assert_not_awaited()
    upload.read.assert_not_called()


async def test_default_tenant_cannot_write_children_of_tenant_b_app(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "TENANT_MODE", "single")
    tenant_b_app = App(
        tenant_id=TENANT_B.tenant_id,
        name="Tenant B private app",
        slug="tenant-b-private-app",
        type="lowcode",
        category="business",
        status="published",
    )
    db_session.add(tenant_b_app)
    await db_session.flush()
    tenant_b_version = AppVersion(
        app_id=tenant_b_app.id,
        version="1.0.0",
        manifest={"name": "Tenant B private manifest"},
        file_url="/tenant-b/private.zip",
        file_hash="b" * 64,
        file_size=1,
        review_status="approved",
    )
    db_session.add(tenant_b_version)
    await db_session.flush()

    with pytest.raises(AppNotFoundException):
        await permission_service.list_by_app(
            db_session,
            app_id=tenant_b_app.id,
            tenant=DEFAULT_TENANT,
        )
    with pytest.raises(AppNotFoundException):
        await version_service.create(
            db_session,
            app_id=tenant_b_app.id,
            version="1.0.1",
            manifest={"name": "blocked"},
            file_url="/blocked.zip",
            file_hash="0" * 64,
            tenant=DEFAULT_TENANT,
        )
    with pytest.raises(AppNotFoundException):
        await permission_service.bulk_insert(
            db_session,
            app_id=tenant_b_app.id,
            permissions=[{"type": "api", "detail": {"path": "/blocked"}}],
            tenant=DEFAULT_TENANT,
        )
    with pytest.raises(AppNotFoundException):
        await rating_service.create(
            db_session,
            RatingCreate(app_id=str(tenant_b_app.id), rating=5),
            user_id=DEFAULT_TENANT.actor_user_id,
            tenant=DEFAULT_TENANT,
        )
    with pytest.raises(AppNotFoundException):
        await review_service.create_pending(
            db_session,
            app_id=tenant_b_app.id,
            version_id=tenant_b_version.id,
            rule_check_result={"manifest_valid": True},
            tenant=DEFAULT_TENANT,
        )

    for model in (AppPermission, AppRating, AppReview):
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.app_id == tenant_b_app.id)
            )
        ) == 0
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AppVersion)
            .where(AppVersion.app_id == tenant_b_app.id)
        )
    ) == 1


async def test_default_tenant_contributes_ignores_cross_tenant_install_link(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "TENANT_MODE", "single")
    tenant_b_app = App(
        tenant_id=TENANT_B.tenant_id,
        name="Tenant B contributed app",
        slug="tenant-b-contributed-app",
        type="lowcode",
        category="business",
        status="published",
    )
    db_session.add(tenant_b_app)
    await db_session.flush()
    version = AppVersion(
        app_id=tenant_b_app.id,
        version="1.0.0",
        manifest={
            "menus": [{"title": "Tenant B secret menu", "page_key": "list"}],
            "pages": [{"key": "list", "title": "Tenant B secret page"}],
        },
        file_url="/tenant-b/private.zip",
        file_hash="b" * 64,
        review_status="approved",
    )
    db_session.add(version)
    await db_session.flush()
    tenant_b_app.current_version_id = version.id
    db_session.add(
        TenantApp(
            tenant_id=DEFAULT_TENANT.tenant_id,
            app_id=tenant_b_app.id,
            installed_version=version.version,
            status="enabled",
        )
    )
    await db_session.flush()

    result = await contributes_service.aggregate_for_tenant(
        db_session,
        tenant=DEFAULT_TENANT,
    )

    assert result == {"menus": [], "pages": []}


async def test_review_detail_rejects_cross_app_version_link(db_session, monkeypatch):
    monkeypatch.setattr(settings, "TENANT_MODE", "single")
    tenant_a_app = App(
        tenant_id=DEFAULT_TENANT.tenant_id,
        name="Tenant A app",
        slug="tenant-a-review-app",
        type="lowcode",
        category="business",
        status="published",
    )
    tenant_b_app = App(
        tenant_id=TENANT_B.tenant_id,
        name="Tenant B app",
        slug="tenant-b-review-app",
        type="lowcode",
        category="business",
        status="published",
    )
    db_session.add_all([tenant_a_app, tenant_b_app])
    await db_session.flush()
    tenant_b_version = AppVersion(
        app_id=tenant_b_app.id,
        version="9.9.9",
        manifest={"secret": "tenant-b"},
        file_url="/tenant-b/private.zip",
        file_hash="b" * 64,
        review_status="pending",
    )
    db_session.add(tenant_b_version)
    await db_session.flush()

    with pytest.raises(AppNotFoundException):
        await review_service.create_pending(
            db_session,
            app_id=tenant_a_app.id,
            version_id=tenant_b_version.id,
            rule_check_result={"manifest_valid": True},
            tenant=DEFAULT_TENANT,
        )

    malformed_review = AppReview(
        app_id=tenant_a_app.id,
        version_id=tenant_b_version.id,
        final_status="pending",
        human_status="pending",
    )
    db_session.add(malformed_review)
    await db_session.flush()

    with pytest.raises(NotFoundException):
        await review_service.get_detail(
            db_session,
            review_id=malformed_review.id,
            tenant=DEFAULT_TENANT,
        )
