from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings, settings
from app.core.tenant import is_tenant_runtime_enabled
from app.modules.platform.constants import PLATFORM_TENANT_ACTIVATE
from app.modules.system.service.tenant_lifecycle_service import tenant_lifecycle_service
from tests.modules.platform.test_tenant_administration import _platform


def test_hosted_supports_multiple_tenants_without_canary(monkeypatch):
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    monkeypatch.setattr(settings, "TENANT_HOSTED_CANARY_TENANT_ID", None)
    assert all(is_tenant_runtime_enabled(tid) for tid in (0, 11, 22))
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", False)
    assert is_tenant_runtime_enabled(0)
    assert not is_tenant_runtime_enabled(11)


def test_hosted_configuration_allows_no_canary_and_emergency_close():
    for enabled in (True, False):
        config = Settings(
            DATABASE_URL="postgresql+asyncpg://localhost/test",
            SECRET_KEY="test-only",
            TENANT_MODE="hosted",
            TENANT_HOSTED_LOGIN_ENABLED=enabled,
            TENANT_HOSTED_CANARY_TENANT_ID=None,
        )
        assert config.TENANT_MODE == "hosted"


@pytest.mark.parametrize("state", ["prepared", "disabled"])
async def test_any_bootstrapped_tenant_can_activate(monkeypatch, state):
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    monkeypatch.setattr(settings, "TENANT_HOSTED_CANARY_TENANT_ID", 11)
    tenant = SimpleNamespace(lifecycle_state=state, status="2", bootstrap_version=1)
    db = AsyncMock()
    db.scalar.return_value = tenant
    result = await tenant_lifecycle_service.activate_tenant(
        db, tenant_id=22, platform=_platform(PLATFORM_TENANT_ACTIVATE, 22)
    )
    assert result.status == "1"
    assert result.lifecycle_state == "active"
