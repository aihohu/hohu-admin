"""Tenant isolation contracts for AI IP abuse protection."""

from unittest.mock import AsyncMock

from tenant_helpers import tenant_context

from app.modules.ai.agents.safety import ip_blacklist


def test_ip_blacklist_keys_are_tenant_scoped() -> None:
    tenant_a = tenant_context(tenant_id=11)
    tenant_b = tenant_context(tenant_id=12)

    assert ip_blacklist._count_key(
        "192.0.2.1", "2026090410", tenant=tenant_a
    ) != ip_blacklist._count_key("192.0.2.1", "2026090410", tenant=tenant_b)
    assert ip_blacklist._blacklist_key(
        "192.0.2.1", tenant=tenant_a
    ) != ip_blacklist._blacklist_key("192.0.2.1", tenant=tenant_b)


async def test_ip_allowlist_memory_cache_is_tenant_scoped(monkeypatch) -> None:
    tenant_a = tenant_context(tenant_id=21)
    tenant_b = tenant_context(tenant_id=22)
    loader = AsyncMock(side_effect=['["192.0.2.1"]', '["198.51.100.2"]'])
    monkeypatch.setattr(ip_blacklist, "get_ai_config_str", loader)
    ip_blacklist._invalidate_allowlist_cache()

    first = await ip_blacklist._load_allowlist(object(), tenant=tenant_a)
    second = await ip_blacklist._load_allowlist(object(), tenant=tenant_b)

    assert first == ["192.0.2.1"]
    assert second == ["198.51.100.2"]
    assert loader.await_count == 2
