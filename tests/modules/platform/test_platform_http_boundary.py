from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.core.base_response import PageResult
from app.core.security import create_access_token, create_platform_access_token
from app.db.session import get_db
from app.main import app
from app.middleware import platform_audit_middleware
from app.modules.ai.service.agent_admin import agent_admin_service
from app.modules.auth import service as auth_service
from app.modules.platform.constants import (
    PLATFORM_AI_READ,
    PLATFORM_SUPPORT_READ,
    PLATFORM_TENANT_BOOTSTRAP,
    PLATFORM_TENANT_WRITE,
)
from app.modules.platform.service import platform_auth_service
from app.modules.platform.tenant_bootstrap_service import (
    TenantBootstrapResult,
    tenant_bootstrap_service,
)
from app.modules.system.service.tenant_lifecycle_service import (
    tenant_lifecycle_service,
)
from app.modules.system.service.tenant_support_service import (
    SupportAuditProjection,
    tenant_support_service,
)


def _platform_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Platform-Reason": "Review AI Agent configuration",
        "X-Platform-Ticket": "SEC-HTTP-1",
        "X-Correlation-ID": "platform-http-1",
    }


async def test_platform_dependency_audits_route_template_not_raw_dynamic_path(
    monkeypatch,
):
    principal = SimpleNamespace(
        principal_id=80,
        principal_name="platform-auditor",
        status="1",
        row_version=1,
        permissions=[PLATFORM_AI_READ],
    )
    db = AsyncMock()
    db.scalar.return_value = principal
    persisted = AsyncMock(return_value=4001)
    monkeypatch.setattr(auth_service, "persist_platform_audit", persisted)
    raw_path = "/ai/provider/token=abcdefghijklmnop123456"
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": b"apiKey=do-not-store",
            "headers": [
                (b"x-platform-reason", b"Review provider configuration"),
                (b"x-platform-ticket", b"SEC-TEMPLATE-1"),
                (b"x-correlation-id", b"route-template-1"),
                (b"x-forwarded-for", b"203.0.113.99"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "route": SimpleNamespace(path="/ai/provider/{provider_id}"),
        }
    )
    token = create_platform_access_token(subject="80", principal_version=1)

    await auth_service.require_platform_context(
        request,
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        db,
    )

    stored = persisted.await_args.kwargs
    assert stored["path"] == "/ai/provider/{provider_id}"
    assert stored["request_summary"] == {"queryKeyCount": 1}
    assert stored["ip"] == "127.0.0.1"
    assert "abcdefghijklmnop123456" not in str(stored)
    assert "do-not-store" not in str(stored)


async def test_platform_login_http_returns_access_token_only(client, monkeypatch):
    db = AsyncMock()
    authenticate = AsyncMock(return_value="platform-access-token")
    monkeypatch.setattr(platform_auth_service, "authenticate", authenticate)
    app.dependency_overrides[get_db] = lambda: db

    try:
        response = await client.post(
            "/platform/auth/login",
            json={
                "principalName": "platform-operator",
                "password": "a-long-test-password",
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert response.json() == {
        "code": 200,
        "msg": "success",
        "data": {"token": "platform-access-token"},
    }
    authenticate.assert_awaited_once()
    db.commit.assert_awaited_once()


async def test_platform_http_authorizes_before_service_and_appends_completion(
    client, monkeypatch
):
    principal = SimpleNamespace(
        principal_id=81,
        principal_name="platform-auditor",
        status="1",
        row_version=2,
        permissions=[PLATFORM_AI_READ],
    )
    db = AsyncMock()
    db.scalar.return_value = principal
    business = AsyncMock(return_value=[])
    authorized = AsyncMock(return_value=5001)
    completed = AsyncMock(return_value=5002)
    monkeypatch.setattr(agent_admin_service, "list_agents", business)
    monkeypatch.setattr(auth_service, "persist_platform_audit", authorized)
    monkeypatch.setattr(
        platform_audit_middleware, "persist_platform_completion", completed
    )
    app.dependency_overrides[get_db] = lambda: db
    token = create_platform_access_token(subject="81", principal_version=2)

    try:
        response = await client.get(
            "/ai/admin/agents", headers=_platform_headers(token)
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    business.assert_awaited_once()
    authorized.assert_awaited_once()
    assert authorized.await_args.kwargs["event_type"] == "authorized"
    completed.assert_awaited_once()
    assert completed.await_args.kwargs["authorization_audit_id"] == 5001
    assert authorized.await_args.kwargs["request_summary"] == {"queryKeyCount": 0}


async def test_missing_platform_audit_header_has_zero_business_side_effect(
    client, monkeypatch
):
    principal = SimpleNamespace(
        principal_id=82,
        principal_name="platform-auditor",
        status="1",
        row_version=1,
        permissions=[PLATFORM_AI_READ],
    )
    db = AsyncMock()
    db.scalar.return_value = principal
    business = AsyncMock(return_value=[])
    denied = AsyncMock(return_value=5003)
    monkeypatch.setattr(agent_admin_service, "list_agents", business)
    monkeypatch.setattr(auth_service, "persist_platform_audit", denied)
    app.dependency_overrides[get_db] = lambda: db
    token = create_platform_access_token(subject="82", principal_version=1)
    headers = _platform_headers(token)
    headers.pop("X-Platform-Reason")

    try:
        response = await client.get("/ai/admin/agents", headers=headers)
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 400
    assert response.json()["errorCode"] == "PLATFORM_AUDIT_CONTEXT_REQUIRED"
    business.assert_not_awaited()
    denied.assert_awaited_once()
    assert denied.await_args.kwargs["event_type"] == "denied"


async def test_platform_completion_failure_log_does_not_render_exception_secrets(
    client, monkeypatch, caplog
):
    principal = SimpleNamespace(
        principal_id=83,
        principal_name="platform-auditor",
        status="1",
        row_version=1,
        permissions=[PLATFORM_AI_READ],
    )
    db = AsyncMock()
    db.scalar.return_value = principal
    monkeypatch.setattr(agent_admin_service, "list_agents", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        auth_service, "persist_platform_audit", AsyncMock(return_value=9)
    )
    completion = AsyncMock(side_effect=RuntimeError("password=abcdefghijklmnop123456"))
    monkeypatch.setattr(
        platform_audit_middleware, "persist_platform_completion", completion
    )
    app.dependency_overrides[get_db] = lambda: db
    token = create_platform_access_token(subject="83", principal_version=1)

    try:
        response = await client.get(
            "/ai/admin/agents", headers=_platform_headers(token)
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert completion.await_count == 2
    assert "abcdefghijklmnop123456" not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_prepare_tenant_preallocates_and_audits_server_bound_target(
    client, monkeypatch
):
    principal = SimpleNamespace(
        principal_id=84,
        principal_name="tenant-operator",
        status="1",
        row_version=1,
        permissions=[PLATFORM_TENANT_WRITE],
    )
    db = AsyncMock()
    db.scalar.side_effect = [principal, None]
    now = datetime.now(UTC)
    prepared = SimpleNamespace(
        tenant_id=991001,
        tenant_code="tenant-acme",
        tenant_name="Acme",
        status="2",
        lifecycle_state="prepared",
        row_version=1,
        created_at=now,
        updated_at=now,
    )
    prepare = AsyncMock(return_value=prepared)
    authorized = AsyncMock(return_value=5101)
    completed = AsyncMock(return_value=5102)
    monkeypatch.setattr(auth_service, "next_id", lambda: 991001)
    monkeypatch.setattr(tenant_lifecycle_service, "prepare_tenant", prepare)
    monkeypatch.setattr(auth_service, "persist_platform_audit", authorized)
    monkeypatch.setattr(
        platform_audit_middleware, "persist_platform_completion", completed
    )
    app.dependency_overrides[get_db] = lambda: db
    token = create_platform_access_token(subject="84", principal_version=1)
    headers = _platform_headers(token) | {"Idempotency-Key": "tenant-create-http-0001"}

    try:
        response = await client.post(
            "/platform/tenants",
            headers=headers,
            json={"tenantCode": "tenant-acme", "tenantName": "Acme"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    target_id = authorized.await_args.kwargs["target_tenant_id"]
    assert target_id > 0
    assert prepare.await_args.kwargs["tenant_id"] == target_id
    assert prepare.await_args.kwargs["platform"].target_tenant_id == target_id
    body = response.json()["data"]
    assert body["enabled"] is False
    assert body["lifecycleState"] == "prepared"
    assert "status" not in body
    assert completed.await_args.kwargs["target_tenant_id"] == target_id
    assert completed.await_args.kwargs["result_summary"] == {
        "statusCode": 200,
        "recordCount": 1,
    }


async def test_support_http_binds_route_target_and_returns_no_private_fields(
    client, monkeypatch
):
    tenant_id = 991002
    principal = SimpleNamespace(
        principal_id=85,
        principal_name="support-reader",
        status="1",
        row_version=1,
        permissions=[PLATFORM_SUPPORT_READ],
    )
    db = AsyncMock()
    db.scalar.return_value = principal
    page = PageResult(
        records=[
            SupportAuditProjection(
                event_id=7001,
                category="system",
                event_type="update",
                outcome="200",
                duration_ms=4,
                occurred_at=datetime.now(),
            )
        ],
        total=1,
        current=1,
        size=20,
    )
    query = AsyncMock(return_value=page)
    authorized = AsyncMock(return_value=5201)
    completed = AsyncMock(return_value=5202)
    monkeypatch.setattr(tenant_support_service, "list_operation_logs", query)
    monkeypatch.setattr(auth_service, "persist_platform_audit", authorized)
    monkeypatch.setattr(
        platform_audit_middleware, "persist_platform_completion", completed
    )
    app.dependency_overrides[get_db] = lambda: db
    token = create_platform_access_token(subject="85", principal_version=1)

    try:
        response = await client.get(
            f"/platform/tenants/{tenant_id}/support/operation-logs",
            headers=_platform_headers(token),
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert authorized.await_args.kwargs["target_tenant_id"] == tenant_id
    assert query.await_args.kwargs["tenant_id"] == tenant_id
    record = response.json()["data"]["records"][0]
    assert set(record) == {
        "eventId",
        "category",
        "eventType",
        "outcome",
        "durationMs",
        "occurredAt",
    }
    assert completed.await_args.kwargs["result_summary"] == {
        "statusCode": 200,
        "recordCount": 1,
    }


async def test_tenant_access_token_cannot_reach_platform_tenant_registry(
    client, monkeypatch
):
    business = AsyncMock()
    monkeypatch.setattr(tenant_lifecycle_service, "list_tenants", business)
    token = create_access_token(subject="1", tenant_id=0)

    response = await client.get(
        "/platform/tenants",
        headers=_platform_headers(token),
    )

    assert response.status_code == 403
    assert response.json()["errorCode"] == "PLATFORM_ADMIN_REQUIRED"
    business.assert_not_awaited()


async def test_platform_support_reader_cannot_prepare_tenant(client, monkeypatch):
    principal = SimpleNamespace(
        principal_id=86,
        principal_name="support-reader",
        status="1",
        row_version=1,
        permissions=[PLATFORM_SUPPORT_READ],
    )
    db = AsyncMock()
    db.scalar.side_effect = [principal, None]
    business = AsyncMock()
    denied = AsyncMock(return_value=5301)
    monkeypatch.setattr(auth_service, "next_id", lambda: 991003)
    monkeypatch.setattr(tenant_lifecycle_service, "prepare_tenant", business)
    monkeypatch.setattr(auth_service, "persist_platform_audit", denied)
    app.dependency_overrides[get_db] = lambda: db
    token = create_platform_access_token(subject="86", principal_version=1)

    try:
        response = await client.post(
            "/platform/tenants",
            headers=_platform_headers(token)
            | {"Idempotency-Key": "tenant-create-http-0002"},
            json={"tenantCode": "tenant-beta", "tenantName": "Beta"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 403
    assert response.json()["errorCode"] == "PLATFORM_PERMISSION_DENIED"
    business.assert_not_awaited()
    assert denied.await_args.kwargs["event_type"] == "denied"
    assert denied.await_args.kwargs["target_tenant_id"] == 991003


async def test_bootstrap_http_keeps_secret_and_machine_ids_out_of_projection(
    client, monkeypatch
):
    tenant_id = 991004
    principal = SimpleNamespace(
        principal_id=87,
        principal_name="tenant-bootstrapper",
        status="1",
        row_version=1,
        permissions=[PLATFORM_TENANT_BOOTSTRAP],
    )
    db = AsyncMock()
    db.scalar.return_value = principal
    bootstrap = AsyncMock(
        return_value=TenantBootstrapResult(
            tenant_code="tenant-gamma",
            lifecycle_state="prepared",
            admin_username="admin",
            model_label="Safe Provider / Chat Model",
            menu_count=47,
            role_count=2,
            model_policy_count=1,
            agent_binding_count=4,
            replayed=False,
        )
    )
    authorized = AsyncMock(return_value=5401)
    completed = AsyncMock(return_value=5402)
    monkeypatch.setattr(tenant_bootstrap_service, "bootstrap", bootstrap)
    monkeypatch.setattr(auth_service, "persist_platform_audit", authorized)
    monkeypatch.setattr(
        platform_audit_middleware, "persist_platform_completion", completed
    )
    app.dependency_overrides[get_db] = lambda: db
    token = create_platform_access_token(subject="87", principal_version=1)
    raw_password = "TenantAdmin123"
    raw_key = "tenant-bootstrap-http-secret-key"

    try:
        response = await client.post(
            f"/platform/tenants/{tenant_id}/bootstrap",
            headers=_platform_headers(token) | {"Idempotency-Key": raw_key},
            json={
                "defaultModelId": "88001",
                "adminPassword": raw_password,
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == {
        "tenantCode",
        "lifecycleState",
        "bootstrapStatus",
        "adminUsername",
        "modelLabel",
        "menuCount",
        "roleCount",
        "modelPolicyCount",
        "agentBindingCount",
        "replayed",
    }
    assert raw_password not in response.text
    assert raw_key not in response.text
    assert "88001" not in response.text
    assert authorized.await_args.kwargs["target_tenant_id"] == tenant_id
    assert authorized.await_args.kwargs["request_summary"] == {"queryKeyCount": 0}
    assert completed.await_args.kwargs["result_summary"] == {
        "statusCode": 200,
        "recordCount": 1,
    }


async def test_tenant_writer_cannot_reach_bootstrap_endpoint(client, monkeypatch):
    tenant_id = 991005
    principal = SimpleNamespace(
        principal_id=88,
        principal_name="tenant-writer",
        status="1",
        row_version=1,
        permissions=[PLATFORM_TENANT_WRITE],
    )
    db = AsyncMock()
    db.scalar.return_value = principal
    business = AsyncMock()
    denied = AsyncMock(return_value=5501)
    monkeypatch.setattr(tenant_bootstrap_service, "bootstrap", business)
    monkeypatch.setattr(auth_service, "persist_platform_audit", denied)
    app.dependency_overrides[get_db] = lambda: db
    token = create_platform_access_token(subject="88", principal_version=1)

    try:
        response = await client.post(
            f"/platform/tenants/{tenant_id}/bootstrap",
            headers=_platform_headers(token)
            | {"Idempotency-Key": "tenant-bootstrap-http-denied"},
            json={
                "defaultModelId": "88002",
                "adminPassword": "TenantAdmin123",
            },
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 403
    assert response.json()["errorCode"] == "PLATFORM_PERMISSION_DENIED"
    business.assert_not_awaited()
    assert denied.await_args.kwargs["permission"] == PLATFORM_TENANT_BOOTSTRAP
    assert denied.await_args.kwargs["target_tenant_id"] == tenant_id
