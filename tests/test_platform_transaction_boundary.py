"""Atomic platform business/audit transaction boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from starlette.requests import Request

from app.core.exceptions import BusinessException
from app.db import session as session_module
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.modules.platform import audit as platform_audit
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.authorization_transaction_service import _Transaction
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


def _request() -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/platform/tenants/1/disable",
            "headers": [],
        }
    )
    request.state.platform_authorization = SimpleNamespace()
    return request


async def test_get_db_commits_platform_completion_with_business(monkeypatch) -> None:
    db = AsyncMock()
    stage = AsyncMock(return_value=7001)
    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: db)
    monkeypatch.setattr(platform_audit, "stage_platform_success_completion", stage)
    request = _request()
    dependency = session_module.get_db(request)

    assert await anext(dependency) is db
    with pytest.raises(StopAsyncIteration):
        await anext(dependency)

    stage.assert_awaited_once_with(db, request=request)
    db.commit.assert_awaited_once()
    assert request.state.platform_completion_committed is True


async def test_get_db_rolls_back_when_platform_completion_cannot_stage(
    monkeypatch,
) -> None:
    db = AsyncMock()
    stage = AsyncMock(
        side_effect=BusinessException(
            code=503,
            message="平台完成审计暂不可用",
            error_code="PLATFORM_AUDIT_UNAVAILABLE",
        )
    )
    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: db)
    monkeypatch.setattr(platform_audit, "stage_platform_success_completion", stage)
    dependency = session_module.get_db(_request())

    assert await anext(dependency) is db
    with pytest.raises(BusinessException) as exc_info:
        await anext(dependency)

    assert exc_info.value.error_code == "PLATFORM_AUDIT_UNAVAILABLE"
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()


@pytest.mark.parametrize("action", ["activate", "disable"])
@pytest.mark.parametrize("failure", [None, "audit", "before_ack", "after_ack"])
async def test_platform_response_waits_for_business_and_audit_commit(
    tenant_http, monkeypatch, action, failure
):
    """Actual independent connections observe known success versus unknown ACK."""
    original, plain = tenant_http
    before = "2" if action == "activate" else "1"
    after = "1" if action == "activate" else "2"
    async with plain() as db:
        await db.execute(
            update(Tenant)
            .where(Tenant.tenant_id == 1)
            .values(
                status=before,
                lifecycle_state="prepared" if action == "activate" else "active",
            )
        )
        await db.commit()
    actual_commit = AuthorizationPlanSession.commit
    actual_append = _Transaction.append_audit
    commits = []

    async def commit(db):
        commits.append(db)
        if len(commits) == 2 and failure == "before_ack":
            raise ConnectionError("Commit connection lost")
        await actual_commit(db)
        if len(commits) == 2 and failure == "after_ack":
            raise ConnectionError("Commit response lost")

    async def append(tx, key, values):
        if (
            failure == "audit"
            and values.get("action") == "completed"
            and values.get("status_code") == 200
        ):
            raise BusinessException(
                503,
                "Audit storage unavailable",
                error_code="PLATFORM_AUDIT_UNAVAILABLE",
            )
        return await actual_append(tx, key, values)

    monkeypatch.setattr(AuthorizationPlanSession, "commit", commit)
    monkeypatch.setattr(_Transaction, "append_audit", append)
    observed = []

    async def observed_app(scope, receive, send):
        async def observe(message):
            if message["type"] == "http.response.start":
                async with plain() as reader:
                    status = await reader.scalar(
                        select(Tenant.status).where(Tenant.tenant_id == 1)
                    )
                    logs = (
                        (
                            await reader.execute(
                                select(SysOperationLog).order_by(
                                    SysOperationLog.operation_log_id
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    observed.append(
                        (
                            message["status"],
                            status,
                            [(row.action, row.status_code) for row in logs],
                        )
                    )
            await send(message)

        await original._transport.app(scope, receive, observe)

    async with AsyncClient(
        transport=ASGITransport(app=observed_app),
        base_url="http://test",
        headers=original.headers,
    ) as client:
        response = await client.post(f"/platform/tenants/1/{action}")
    assert len(commits) == 2, "No second completion or retry after an unknown ACK"
    if failure is None:
        assert observed == [(200, after, [("authorized", None), ("completed", 200)])]
    elif failure == "audit":
        assert response.status_code == 503, response.text
        assert observed == [(503, before, [("authorized", None), ("completed", 503)])]
    else:
        assert response.status_code == 503, response.text
        assert response.json()["errorCode"] == "AUTHORIZATION_COMMIT_UNKNOWN"
        assert observed == [
            (
                503,
                after if failure == "after_ack" else before,
                [("authorized", None)]
                + ([("completed", 200)] if failure == "after_ack" else []),
            )
        ]
