import pytest

from app.core.exceptions import AuthorizationException
from app.modules.marketplace.api.app_data import _resolve_table_and_schema
from app.modules.marketplace.exceptions import (
    AppInvalidManifestException,
    AppNotFoundException,
)
from app.modules.marketplace.models import App, AppVersion, TenantApp
from tests.tenant_helpers import tenant_context


async def _seed_installed_app(
    db,
    *,
    slug: str,
    status: str = "enabled",
    manifest: dict | None = None,
):
    manifest = manifest or {
        "name": "Safe",
        "slug": slug,
        "version": "1.0.0",
        "type": "lowcode",
        "category": "business",
        "models": [
            {
                "key": "customer",
                "data_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                },
            }
        ],
    }
    app = App(
        tenant_id=0,
        name="Safe",
        slug=slug,
        type="lowcode",
        category="business",
        status="published",
    )
    db.add(app)
    await db.flush()
    version = AppVersion(
        app_id=app.id,
        version="1.0.0",
        manifest=manifest,
        file_url="/safe.zip",
        file_hash="a" * 64,
        review_status="approved",
    )
    db.add(version)
    db.add(
        TenantApp(
            tenant_id=0,
            app_id=app.id,
            installed_version="1.0.0",
            status=status,
        )
    )
    await db.flush()
    return app


async def test_resolution_requires_enabled_installation(db_session):
    await _seed_installed_app(db_session, slug="disabled-resolution", status="disabled")
    with pytest.raises(AppNotFoundException):
        await _resolve_table_and_schema(
            db_session,
            slug="disabled-resolution",
            model="customer",
            tenant=tenant_context(),
        )


async def test_resolution_rejects_unknown_model_without_constructing_table(db_session):
    await _seed_installed_app(db_session, slug="unknown-resolution")
    with pytest.raises(AppNotFoundException):
        await _resolve_table_and_schema(
            db_session,
            slug="unknown-resolution",
            model="customer;drop",
            tenant=tenant_context(),
        )


async def test_resolution_fails_closed_for_legacy_invalid_manifest(db_session):
    manifest = {
        "name": "Legacy",
        "slug": "legacy-resolution",
        "version": "1.0.0",
        "type": "lowcode",
        "category": "business",
        "models": [
            {
                "key": "customer",
                "data_schema": {
                    "type": "object",
                    "properties": {"name;drop": {"type": "string"}},
                },
            }
        ],
    }
    await _seed_installed_app(db_session, slug="legacy-resolution", manifest=manifest)
    with pytest.raises(AppInvalidManifestException):
        await _resolve_table_and_schema(
            db_session,
            slug="legacy-resolution",
            model="customer",
            tenant=tenant_context(),
        )


async def test_resolution_does_not_cross_tenant_boundary(db_session):
    await _seed_installed_app(db_session, slug="tenant-resolution")
    with pytest.raises(AuthorizationException):
        await _resolve_table_and_schema(
            db_session,
            slug="tenant-resolution",
            model="customer",
            tenant=tenant_context(tenant_id=99),
        )
