import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select, update
from starlette.requests import Request

from app.core.security import create_access_token, create_platform_access_token
from app.db.session import get_db
from app.main import app
from app.modules.ai.service.tenant_model_policy_admin_service import (
    tenant_model_policy_admin_service,
)
from app.modules.auth import service as auth_service
from app.modules.auth.service import get_current_user
from app.modules.platform import api as platform_api
from app.modules.platform.authorization_principal_audit import PrincipalAudit
from app.modules.platform.constants import (
    PLATFORM_AI_READ,
    PLATFORM_SUPPORT_READ,
    PLATFORM_TENANT_ACTIVATE,
    PLATFORM_TENANT_BOOTSTRAP,
    PLATFORM_TENANT_WRITE,
)
from app.modules.platform.models import PlatformAuditLog
from app.modules.platform.tenant_bootstrap_service import (
    tenant_bootstrap_service,
)
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.tenant_lifecycle_service import (
    tenant_lifecycle_service,
)
from app.modules.system.service.tenant_support_service import (
    tenant_support_service,
)
from tests.modules.platform.test_authorization_principal_control_http import (
    principal_control_http as principal_control_http,
)
from tests.modules.platform.test_authorization_principal_control_http import (
    principal_http as principal_http,
)
from tests.modules.platform.test_authorization_principal_control_http import (
    principal_sessions as principal_sessions,
)
from tests.modules.platform.test_authorization_tenant_http import (
    catalog_http as catalog_http,
)
from tests.modules.platform.test_authorization_tenant_http import (
    catalog_sessions as catalog_sessions,
)
from tests.modules.platform.test_authorization_tenant_http import (
    http_system as http_system,
)
from tests.modules.platform.test_authorization_tenant_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.platform.test_authorization_tenant_http import (
    system_sessions as system_sessions,
)
from tests.modules.platform.test_authorization_tenant_http import (
    tenant_http as tenant_http,
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
    raw_path = "/platform/ai/providers/token=abcdefghijklmnop123456"
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
            "route": SimpleNamespace(path="/platform/ai/providers/{provider_id}"),
        }
    )
    token = create_platform_access_token(subject="80", principal_version=1)

    await auth_service.require_platform_context(
        request,
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        db,
    )

    stored = persisted.await_args.kwargs
    assert stored["path"] == "/platform/ai/providers/{provider_id}"
    assert stored["request_summary"] == {"queryKeyCount": 1}
    assert stored["ip"] == "127.0.0.1"
    assert "abcdefghijklmnop123456" not in str(stored)
    assert "do-not-store" not in str(stored)


async def test_platform_login_http_returns_access_token_only(client, monkeypatch):
    db = AsyncMock()
    authenticate = AsyncMock(return_value="platform-access-token")
    monkeypatch.setattr(
        platform_api,
        "get_authorization_runtime",
        lambda: SimpleNamespace(principal_login=authenticate),
    )
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
    authenticate.assert_awaited_once_with(
        principal_name="platform-operator", password="a-long-test-password"
    )
    db.commit.assert_not_awaited()


async def test_platform_http_authorizes_before_service_and_appends_completion(
    principal_control_http, monkeypatch
):
    client, plain = principal_control_http
    original = tenant_support_service.list_operation_logs
    observed = []

    async def business(db, **kwargs):
        async with plain() as observer:
            rows = (await observer.execute(select(PlatformAuditLog))).scalars().all()
            assert len(rows) == 1 and rows[0].event_type == "authorized"
        observed.append(kwargs["tenant_id"])
        return await original(db, **kwargs)

    monkeypatch.setattr(tenant_support_service, "list_operation_logs", business)
    response = await client.get("/platform/tenants/1/support/operation-logs")
    assert response.status_code == 200, response.text
    assert observed == [1]
    async with plain() as db:
        rows = (
            (
                await db.execute(
                    select(PlatformAuditLog).order_by(PlatformAuditLog.audit_id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.event_type for row in rows] == ["authorized", "completed"]
        assert rows[1].authorization_audit_id == rows[0].audit_id


async def test_missing_platform_audit_header_has_zero_business_side_effect(
    principal_control_http, monkeypatch
):
    client, plain = principal_control_http
    business = AsyncMock()
    monkeypatch.setattr(tenant_support_service, "list_operation_logs", business)
    del client.headers["X-Platform-Reason"]
    response = await client.get("/platform/tenants/1/support/operation-logs")
    assert response.status_code == 400, response.text
    assert response.json()["errorCode"] == "PLATFORM_AUDIT_CONTEXT_REQUIRED"
    business.assert_not_awaited()
    async with plain() as db:
        rows = (await db.execute(select(PlatformAuditLog))).scalars().all()
        assert len(rows) == 1 and rows[0].event_type == "denied"


async def test_platform_completion_failure_log_does_not_render_exception_secrets(
    principal_control_http, monkeypatch, caplog
):
    client, plain = principal_control_http
    completion = AsyncMock(side_effect=RuntimeError("password=abcdefghijklmnop123456"))
    monkeypatch.setattr(PrincipalAudit, "append", completion)
    response = await client.get("/platform/tenants/1/support/operation-logs")
    assert response.status_code == 503, response.text
    assert response.json()["errorCode"] == "PLATFORM_AUDIT_UNAVAILABLE"
    assert completion.await_count == 2
    async with plain() as db:
        rows = (await db.execute(select(PlatformAuditLog))).scalars().all()
        assert len(rows) == 1 and rows[0].event_type == "authorized"
    assert "abcdefghijklmnop123456" not in caplog.text
    assert "abcdefghijklmnop123456" not in response.text


async def test_prepare_tenant_audits_target_allocated_under_guard(tenant_http):
    client, plain = tenant_http
    response = await client.post(
        "/platform/tenants",
        headers={"Idempotency-Key": "tenant-create-http-0001"},
        json={"tenantCode": "tenant-acme", "tenantName": "Acme"},
    )
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["enabled"] is False
    assert body["lifecycleState"] == "prepared"
    assert "status" not in body
    async with plain() as db:
        logs = (
            (
                await db.execute(
                    select(SysOperationLog).order_by(SysOperationLog.operation_log_id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.action for row in logs] == ["authorized", "completed"]
        assert json.loads(logs[0].request_params)["target_tenant_id"] is None
        assert (
            json.loads(logs[1].request_params)["result_summary"]["tenantId"]
            == body["tenantId"]
        )


async def test_support_http_binds_route_target_and_returns_no_private_fields(
    principal_control_http,
):
    client, plain = principal_control_http
    response = await client.get("/platform/tenants/1/support/operation-logs")
    assert response.status_code == 200, response.text
    item = response.json()["data"]["records"][0]
    assert item["eventId"] == "900"
    assert "userId" not in item and "requestParams" not in item and "ip" not in item
    async with plain() as db:
        rows = (await db.execute(select(PlatformAuditLog))).scalars().all()
        assert len(rows) == 2 and all(row.target_tenant_id == 1 for row in rows)


async def test_tenant_access_token_cannot_reach_platform_tenant_registry(
    client, monkeypatch
):
    actor = SimpleNamespace(
        user_id=123,
        tenant_id=7,
        user_name="admin",
        status="1",
        roles=[SimpleNamespace(tenant_id=7, role_code="R_SUPER", status="1")],
    )
    app.dependency_overrides[get_current_user] = lambda: actor
    business = AsyncMock()
    monkeypatch.setattr(tenant_lifecycle_service, "list_tenants", business)
    token = create_access_token(
        subject="1", tenant_id=0, tenant_version=1, user_version=1
    )

    try:
        response = await client.get(
            "/platform/tenants",
            headers=_platform_headers(token),
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 403
    assert response.json()["errorCode"] == "SYSTEM_ADMIN_ONLY"
    business.assert_not_awaited()


async def test_tenant_access_token_cannot_mutate_tenant_model_policy(
    client, monkeypatch
):
    actor = SimpleNamespace(
        user_id=123,
        tenant_id=7,
        user_name="admin",
        status="1",
        roles=[SimpleNamespace(tenant_id=7, role_code="R_SUPER", status="1")],
    )
    app.dependency_overrides[get_current_user] = lambda: actor
    business = AsyncMock()
    monkeypatch.setattr(tenant_model_policy_admin_service, "put", business)
    token = create_access_token(
        subject="1", tenant_id=0, tenant_version=1, user_version=1
    )

    try:
        response = await client.put(
            "/platform/tenants/9001/ai/model-policies/8001",
            headers=_platform_headers(token),
            json={"enabled": True, "isDefault": True},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 403
    assert response.json()["errorCode"] == "SYSTEM_ADMIN_ONLY"
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

    assert response.status_code == 401
    assert response.json()["errorCode"] == "TOKEN_EXPIRED"
    business.assert_not_awaited()

    denied.assert_not_awaited()


async def test_bootstrap_http_keeps_secret_and_machine_ids_out_of_projection(
    tenant_http,
):
    client, plain = tenant_http
    created = await client.post(
        "/platform/tenants",
        headers={"Idempotency-Key": "tenant-create-bootstrap-key"},
        json={"tenantCode": "tenant-gamma", "tenantName": "Gamma"},
    )
    assert created.status_code == 200, created.text
    tenant_id = created.json()["data"]["tenantId"]
    raw_password, raw_key = "TenantAdmin123", "tenant-bootstrap-http-secret-key"
    response = await client.post(
        f"/platform/tenants/{tenant_id}/bootstrap",
        headers={"Idempotency-Key": raw_key},
        json={"defaultModelId": "601", "adminPassword": raw_password},
    )
    assert response.status_code == 200, response.text
    assert set(response.json()["data"]) == {
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
    assert raw_password not in response.text and raw_key not in response.text
    assert "601" not in response.text
    async with plain() as db:
        rows = (
            (
                await db.execute(
                    select(SysOperationLog).where(
                        SysOperationLog.path.endswith("/bootstrap")
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        for row in rows:
            metadata = json.loads(row.request_params)
            assert metadata["target_tenant_id"] == int(tenant_id)
            assert metadata["permission"] == PLATFORM_TENANT_BOOTSTRAP
            assert (
                raw_password not in row.request_params
                and raw_key not in row.request_params
            )


async def test_activate_http_uses_dedicated_permission_and_bound_target(tenant_http):
    client, plain = tenant_http
    async with plain() as db:
        await db.execute(
            update(Tenant)
            .where(Tenant.tenant_id == 1)
            .values(status="2", lifecycle_state="prepared", bootstrap_version=1)
        )
        await db.commit()
    response = await client.post("/platform/tenants/1/activate")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["enabled"] is True and data["lifecycleState"] == "active"
    assert data["bootstrapStatus"] == "ready"
    async with plain() as db:
        rows = (await db.execute(select(SysOperationLog))).scalars().all()
        assert len(rows) == 2
        for row in rows:
            metadata = json.loads(row.request_params)
            assert metadata["permission"] == PLATFORM_TENANT_ACTIVATE
            assert metadata["target_tenant_id"] == 1


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

    assert response.status_code == 401
    assert response.json()["errorCode"] == "TOKEN_EXPIRED"
    business.assert_not_awaited()

    denied.assert_not_awaited()
