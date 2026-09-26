import inspect
from unittest.mock import AsyncMock

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantLocatorContext
from app.modules.system.api.settings import router
from app.modules.system.models.config import Config
from app.modules.system.models.setting import SystemSetting
from app.modules.system.service.config_service import config_service
from app.modules.system.service.settings_service import settings_service
from tests.tenant_helpers import create_test_tenant, tenant_context


async def test_settings_write_has_no_custom_parameter_side_effect(db_session):
    row = await create_test_tenant(db_session, prefix="sett")
    tenant = tenant_context(tenant_id=row.tenant_id, actor_user_id=1)
    initial = await settings_service.get_group(db_session, "brand", tenant=tenant)
    await settings_service.save_group(
        db_session,
        "brand",
        {"site_name": "Private brand"},
        revision=initial["revision"],
        tenant=tenant,
    )
    assert (
        await db_session.scalar(
            select(SystemSetting.setting_value).where(
                SystemSetting.tenant_id == row.tenant_id,
                SystemSetting.setting_key == "site_name",
            )
        )
        == "Private brand"
    )
    assert (
        await db_session.scalar(select(Config).where(Config.tenant_id == row.tenant_id))
        is None
    )
    locator = TenantLocatorContext(row.tenant_id, row.tenant_code, row.row_version)
    assert "site_name" not in await config_service.get_public_configs.__wrapped__(
        config_service, db_session, tenant=locator
    )
    assert (await settings_service.get_public_values(db_session, tenant=locator))[
        "site_name"
    ] == "Private brand"
    assert (
        await config_service.get_value.__wrapped__(
            config_service, db_session, "site_name", tenant=tenant
        )
        is None
    )


def test_settings_endpoints_require_separate_permissions():
    for route in router.routes:
        if not isinstance(route, APIRoute) or route.path in {"/runtime", "/public"}:
            continue
        codes = set()
        for dependency in route.dependant.dependencies:
            if inspect.isfunction(dependency.call):
                codes.add(
                    inspect.getclosurevars(dependency.call).nonlocals.get("perm_code")
                )
        expected = (
            "system:setting:edit" if "PUT" in route.methods else "system:setting:list"
        )
        assert expected in codes
        assert "system:config:edit" not in codes
        assert "system:config:list" not in codes


async def test_custom_parameter_create_rejects_system_settings_before_database_access():
    db = AsyncMock()
    with pytest.raises(BusinessRuleException, match="built-in"):
        await config_service.create(
            db,
            type("Input", (), {"config_key": "site_name"})(),
            tenant=tenant_context(),
        )
    db.execute.assert_not_awaited()
