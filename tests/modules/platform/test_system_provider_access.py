"""Model administration uses the normal system role and user audit identity."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.base_response import PageResult
from app.core.security import create_platform_access_token
from app.db.session import get_db
from app.main import app
from app.modules.ai.service.provider_service import provider_service
from app.modules.auth.api import get_user_routes, is_route_exist
from app.modules.auth.service import get_current_user
from app.modules.platform import system_agent_auth


def _actor(tenant=0, role=True, status="1"):
    return SimpleNamespace(
        tenant_id=tenant,
        user_id=123,
        user_name="renamed-owner",
        status=status,
        roles=[SimpleNamespace(tenant_id=tenant, role_code="R_SUPER", status="1")]
        if role
        else [],
    )


def _headers():
    return {
        "X-Platform-Reason": "Configure models",
        "X-Platform-Ticket": "MODEL-1",
        "X-Correlation-ID": "model-1",
    }


@pytest.mark.parametrize(
    "tenant,role,status", [(12, True, "1"), (0, False, "1"), (0, True, "2")]
)
@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "/platform/ai/providers", None),
        ("GET", "/platform/ai/providers/models", None),
        (
            "POST",
            "/platform/ai/providers",
            {"providerCode": "test", "name": "Test", "apiKey": "key"},
        ),
        ("PUT", "/platform/ai/providers/101", {"name": "Changed"}),
        ("DELETE", "/platform/ai/providers/101", None),
        ("GET", "/platform/ai/providers/101/models", None),
        (
            "POST",
            "/platform/ai/providers/101/models",
            {"name": "model", "capabilities": ["text"]},
        ),
        ("PUT", "/platform/ai/providers/101/models/201", {"name": "changed"}),
        ("DELETE", "/platform/ai/providers/101/models/201", None),
        ("POST", "/platform/ai/providers/101/test", {"modelId": "201"}),
    ],
)
async def test_provider_routes_reject_non_system_roles(
    client, monkeypatch, tenant, role, status, method, path, payload
):
    actor = _actor(tenant, role, status)
    business = AsyncMock()
    monkeypatch.setattr(provider_service, "get_list", business)
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        response = await client.request(method, path, json=payload, headers=_headers())
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 403
    assert response.json()["errorCode"] == "SYSTEM_ADMIN_ONLY"
    business.assert_not_awaited()


async def test_provider_list_accepts_normal_system_role_and_records_user(
    client, monkeypatch
):
    db = AsyncMock()
    db.add = Mock()
    business = AsyncMock(
        return_value=PageResult(records=[], total=0, current=1, size=10)
    )
    persist = AsyncMock(return_value=101)
    monkeypatch.setattr(provider_service, "get_list", business)
    monkeypatch.setattr(system_agent_auth, "persist_system_agent_audit", persist)
    app.dependency_overrides[get_current_user] = _actor
    app.dependency_overrides[get_db] = lambda: db
    try:
        response = await client.get("/platform/ai/providers", headers=_headers())
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 200
    business.assert_awaited_once()
    assert persist.await_args.kwargs["user_id"] == 123
    audit = db.add.call_args.args[0]
    assert audit.user_id == 123
    assert audit.module == "模型管理"
    assert audit.audit_scope == "platform"


async def test_old_platform_token_cannot_manage_providers(client):
    token = create_platform_access_token(subject="81", principal_version=1)
    response = await client.get(
        "/platform/ai/providers",
        headers={**_headers(), "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


async def test_provider_missing_audit_context_never_calls_business(client, monkeypatch):
    business = AsyncMock()
    persist = AsyncMock(return_value=101)
    monkeypatch.setattr(provider_service, "get_list", business)
    monkeypatch.setattr(system_agent_auth, "persist_system_agent_audit", persist)
    app.dependency_overrides[get_current_user] = _actor
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        response = await client.get("/platform/ai/providers")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 400
    assert response.json()["errorCode"] == "PLATFORM_AUDIT_CONTEXT_REQUIRED"
    business.assert_not_awaited()


async def test_provider_create_returns_only_credential_state_and_audits_user(
    client, monkeypatch
):
    record = SimpleNamespace(
        provider_id=101,
        provider_code="test",
        name="Test",
        api_key="encrypted-secret",
        base_url=None,
        is_enabled=True,
        config=None,
        create_time=datetime.now(UTC),
    )
    business = AsyncMock(return_value=record)
    db = AsyncMock()
    db.add = Mock()
    monkeypatch.setattr(provider_service, "create", business)
    monkeypatch.setattr(
        system_agent_auth, "persist_system_agent_audit", AsyncMock(return_value=101)
    )
    app.dependency_overrides[get_current_user] = _actor
    app.dependency_overrides[get_db] = lambda: db
    try:
        response = await client.post(
            "/platform/ai/providers",
            json={"providerCode": "test", "name": "Test", "apiKey": "input-secret"},
            headers=_headers(),
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 200
    assert response.json()["data"]["providerId"] == "101"
    assert response.json()["data"]["credentialConfigured"] is True
    assert "apiKey" not in response.json()["data"]
    assert "secret" not in response.text
    assert "secret" not in db.add.call_args.args[0].request_params


@pytest.mark.parametrize(
    "tenant,role,expected", [(0, True, True), (12, True, False), (0, False, False)]
)
async def test_provider_menu_and_route_existence_follow_system_role(
    tenant, role, expected
):
    db = AsyncMock()
    db.execute.return_value.scalars = Mock(return_value=SimpleNamespace(all=lambda: []))
    actor = _actor(tenant, role)
    response = await get_user_routes(current_user=actor, db=db)
    ai = next((r for r in response.data["routes"] if r.name == "ai"), None)
    providers = [r for r in (ai.children if ai else []) if r.name == "ai_provider"]
    assert bool(providers) is expected
    if expected:
        assert providers[0].path == "/ai/provider"
        assert providers[0].meta.title == "模型管理"
    assert (
        await is_route_exist(route_name="ai_provider", current_user=actor, db=db)
    ).data is expected
