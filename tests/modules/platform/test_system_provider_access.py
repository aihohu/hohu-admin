"""Model administration uses the normal system role and user audit identity."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update

from app.core.security import create_platform_access_token
from app.core.tenant import TenantContext
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.base import user_roles
from app.db.session import get_db
from app.main import app
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.service.provider_service import provider_service
from app.modules.auth.service import get_current_user
from app.modules.platform.authorization_catalog_read import catalog_read_candidate
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.role import Role
from tests.modules.platform.test_authorization_catalog_http import (
    catalog_http as catalog_http,
)
from tests.modules.platform.test_authorization_catalog_http import (
    catalog_sessions as catalog_sessions,
)
from tests.modules.platform.test_authorization_catalog_http import (
    http_system as http_system,
)
from tests.modules.platform.test_authorization_catalog_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.platform.test_authorization_catalog_http import (
    system_sessions as system_sessions,
)


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
        (
            "POST",
            "/platform/ai/providers/test",
            {
                "providerCode": "openai",
                "apiKey": "draft-key",
                "model": {"name": "draft-model", "capabilities": ["text"]},
            },
        ),
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
    catalog_http, monkeypatch
):
    client, plain = catalog_http
    monkeypatch.setattr(
        provider_egress, "is_configuration_allowed", AsyncMock(return_value=True)
    )
    response = await client.get("/platform/ai/providers", headers=_headers())
    assert response.status_code == 200, response.text
    async with plain() as db:
        rows = (
            await db.scalars(
                select(SysOperationLog).where(SysOperationLog.audit_scope == "platform")
            )
        ).all()
        assert len(rows) == 2 and all(row.user_id == 101 for row in rows)
        assert {row.action for row in rows} == {"authorized", "completed"}


async def test_old_platform_token_cannot_manage_providers(client):
    token = create_platform_access_token(subject="81", principal_version=1)
    response = await client.get(
        "/platform/ai/providers",
        headers={**_headers(), "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


async def test_provider_missing_audit_context_never_calls_business(
    catalog_http, monkeypatch
):
    client, plain = catalog_http
    business = AsyncMock(side_effect=AssertionError("Denied read cannot project"))
    monkeypatch.setattr(catalog_read_candidate, "project", business)
    client.headers.pop("X-Platform-Reason")
    response = await client.get("/platform/ai/providers")
    assert response.status_code == 400, response.text
    assert response.json()["errorCode"] == "PLATFORM_AUDIT_CONTEXT_REQUIRED"
    business.assert_not_awaited()
    async with plain() as db:
        rows = (
            await db.scalars(
                select(SysOperationLog).where(SysOperationLog.audit_scope == "platform")
            )
        ).all()
        assert len(rows) == 1 and rows[0].action == "denied"


async def test_provider_create_returns_only_credential_state_and_audits_user(
    catalog_http,
):
    client, plain = catalog_http
    response = await client.post(
        "/platform/ai/providers",
        json={"providerCode": "deepseek", "name": "Test", "apiKey": "input-secret"},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    assert isinstance(response.json()["data"]["providerId"], str)
    assert response.json()["data"]["credentialConfigured"] is True
    assert "apiKey" not in response.json()["data"]
    assert "secret" not in response.text
    async with plain() as db:
        audit = (await db.execute(select(SysOperationLog))).scalars().all()
        assert len(audit) == 2
        assert all(row.user_id == 101 for row in audit)
        assert all("input-secret" not in row.request_params for row in audit)


@pytest.mark.parametrize(
    "tenant,role,expected", [(0, True, True), (1, True, False), (0, False, False)]
)
async def test_provider_menu_and_route_existence_follow_system_role(
    system_sessions, tenant, role, expected
):
    _, plain = system_sessions
    uid, rid = 101 + tenant, 201 + tenant
    async with plain() as db:
        await db.execute(
            update(Role)
            .where(Role.tenant_id == tenant, Role.role_id == rid)
            .values(role_code="R_SUPER" if role else "R_USER")
        )
        if tenant:
            await db.execute(
                insert(user_roles).values(tenant_id=tenant, user_id=uid, role_id=rid)
            )
        await db.commit()
    runtime = AuthorizationRuntime(plain.kw["bind"])
    identity = {
        "tenant": TenantContext(tenant, "plan-" + str(tenant), uid, 1, "access_token"),
        "actor_user_version": 1,
    }
    response = await runtime.relation_read(**identity, operation="runtime.routes")
    ai = next((r for r in response["routes"] if r["name"] == "ai"), None)
    providers = [
        r for r in (ai.get("children", []) if ai else []) if r["name"] == "ai_provider"
    ]
    assert bool(providers) is expected
    if expected:
        assert (
            providers[0]["path"] == "/ai/provider"
            and providers[0]["meta"]["title"] == "模型管理"
        )
    assert (
        await runtime.relation_read(
            **identity, operation="runtime.exists", query="ai_provider"
        )
        is expected
    )
