"""Principal support and retention retain exact targets and planned audits."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, insert, select, update

from app.core.security import create_platform_access_token
from app.modules.platform import api, audit
from app.modules.platform.models import PlatformAuditLog, PlatformPrincipal
from app.modules.system.models.operation_log import SysOperationLog
from tests.modules.platform.test_authorization_principal_http import (
    http_system as http_system,
)
from tests.modules.platform.test_authorization_principal_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.platform.test_authorization_principal_http import (
    principal_http as principal_http,
)
from tests.modules.platform.test_authorization_principal_http import (
    principal_sessions as principal_sessions,
)
from tests.modules.platform.test_authorization_principal_http import (
    system_sessions as system_sessions,
)


@pytest.fixture
async def principal_control_http(principal_http, monkeypatch):
    client, plain = principal_http
    client._transport.app.include_router(api.control_router, prefix="/platform")
    monkeypatch.setattr(audit, "AsyncSessionLocal", plain)
    async with plain() as db:
        await db.execute(
            update(PlatformPrincipal).values(
                permissions=["platform:support:read", "platform:audit:retention"]
            )
        )
        for identifier, scope in ((900, "tenant"), (901, "platform")):
            await db.execute(
                insert(SysOperationLog).values(
                    operation_log_id=identifier,
                    tenant_id=1,
                    audit_scope=scope,
                    user_id=102,
                    username="private-person",
                    module="Users",
                    action="update",
                    method="POST",
                    path="/private",
                    request_params="private-password",
                    create_time=datetime.now() - timedelta(days=500),
                )
            )
        await db.commit()
    token = create_platform_access_token(subject="700", principal_version=2)
    client.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "X-Platform-Reason": "Review support",
            "X-Platform-Ticket": "SUPPORT-HTTP",
            "X-Correlation-ID": "principal-control-http",
        }
    )
    yield client, plain


@pytest.mark.parametrize(
    "operation", ["operation-logs", "login-logs", "preview", "purge"]
)
async def test_principal_controls_take_guard_before_resource_locks(
    principal_control_http, operation
):
    client, plain = principal_control_http
    locks, commits = [], []
    engine = plain.kw["bind"].sync_engine

    def sql(_c, _cursor, statement, _params, _ctx, _many):
        if (
            "FOR UPDATE" in statement
            or "FOR SHARE" in statement
            or "advisory" in statement
        ):
            locks.append(statement)

    def commit(connection):
        commits.append(connection)

    event.listen(engine, "before_cursor_execute", sql)
    event.listen(engine, "commit", commit)
    try:
        if operation.endswith("logs"):
            response = await client.get(f"/platform/tenants/1/support/{operation}")
        else:
            payload = {"cutoff": (datetime.now() - timedelta(days=400)).isoformat()}
            if operation == "purge":
                payload.update(expectedOperationCount=1, expectedLoginCount=0)
            response = await client.post(
                f"/platform/tenants/1/audit-retention/{operation}", json=payload
            )
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        event.remove(engine, "commit", commit)
    assert response.status_code == 200, response.text
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    assert not any("advisory" in sql for sql in locks)
    assert len(commits) == 3
    assert (
        "private-person" not in response.text
        and "private-password" not in response.text
    )
    async with plain() as db:
        logs = (
            (
                await db.execute(
                    select(PlatformAuditLog).order_by(PlatformAuditLog.audit_id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.event_type for row in logs] == ["authorized", "completed"]
        assert logs[-1].authorization_audit_id == logs[0].audit_id
        assert all(row.target_tenant_id == 1 for row in logs)
        records = (
            (await db.execute(select(SysOperationLog.operation_log_id))).scalars().all()
        )
        assert set(records) == ({901} if operation == "purge" else {900, 901})


async def test_principal_missing_context_has_planned_denial(principal_control_http):
    client, plain = principal_control_http
    del client.headers["X-Platform-Reason"]
    response = await client.get("/platform/tenants/1/support/operation-logs")
    assert response.status_code == 400, response.text
    async with plain() as db:
        logs = (await db.execute(select(PlatformAuditLog))).scalars().all()
        assert len(logs) == 1 and logs[0].event_type == "denied"
        assert logs[0].denial_code == "PLATFORM_AUDIT_CONTEXT_REQUIRED"


async def test_support_missing_tenant_preserves_not_found(principal_control_http):
    client, plain = principal_control_http
    response = await client.get("/platform/tenants/999/support/login-logs")
    assert response.status_code == 404, response.text
    assert response.json()["errorCode"] == "PLATFORM_TENANT_NOT_FOUND"
    async with plain() as db:
        logs = (
            (
                await db.execute(
                    select(PlatformAuditLog).order_by(PlatformAuditLog.audit_id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.event_type for row in logs] == ["authorized", "completed"]
        assert logs[-1].status_code == 404
