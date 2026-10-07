"""Atomic platform business/audit transaction boundary."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.core.exceptions import BusinessException, setup_exception_handlers
from app.db import session as session_module
from app.modules.auth.service import get_current_user
from app.modules.platform import audit as platform_audit
from app.modules.platform import system_agent_auth
from app.modules.platform.api import control_router
from app.modules.system.service.tenant_lifecycle_service import tenant_lifecycle_service


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


class _SessionContext:
    def __init__(self, db) -> None:
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_args):
        return False


async def test_get_db_commits_platform_completion_with_business(monkeypatch) -> None:
    db = AsyncMock()
    stage = AsyncMock(return_value=7001)
    monkeypatch.setattr(
        session_module, "AsyncSessionLocal", lambda: _SessionContext(db)
    )
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
    monkeypatch.setattr(
        session_module, "AsyncSessionLocal", lambda: _SessionContext(db)
    )
    monkeypatch.setattr(platform_audit, "stage_platform_success_completion", stage)
    dependency = session_module.get_db(_request())

    assert await anext(dependency) is db
    with pytest.raises(BusinessException) as exc_info:
        await anext(dependency)

    assert exc_info.value.error_code == "PLATFORM_AUDIT_UNAVAILABLE"
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()


@pytest.mark.parametrize("action", ["activate", "disable"])
@pytest.mark.parametrize("failure", [None, "audit", "commit"])
async def test_platform_response_waits_for_business_and_audit_commit(
    monkeypatch, action, failure
):
    """Observe response.start: ASGITransport alone waits for late cleanup too."""
    events = []
    db = AsyncMock()
    db.add = Mock(side_effect=lambda _record: events.append("audit"))

    async def commit():
        if failure == "commit":
            raise RuntimeError("database commit unavailable")
        events.append("commit")

    db.commit.side_effect = commit
    if failure == "audit":
        db.flush.side_effect = RuntimeError("audit storage unavailable")
    monkeypatch.setattr(
        session_module, "AsyncSessionLocal", lambda: _SessionContext(db)
    )
    monkeypatch.setattr(
        system_agent_auth, "persist_system_agent_audit", AsyncMock(return_value=100)
    )
    monkeypatch.setattr(
        system_agent_auth, "resolve_platform_target", AsyncMock(return_value=7001)
    )
    tenant = SimpleNamespace(
        tenant_id=7001,
        tenant_code="qualification-b",
        tenant_name="Control",
        status="1" if action == "activate" else "2",
        lifecycle_state="active",
        bootstrap_version=1,
        row_version=2,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    async def change_tenant(session, **_kwargs):
        assert session is db
        events.append("business")
        return tenant

    monkeypatch.setattr(tenant_lifecycle_service, action + "_tenant", change_tenant)
    test_app = FastAPI()
    setup_exception_handlers(test_app)
    test_app.include_router(control_router, prefix="/platform")
    test_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        tenant_id=0,
        user_id=123,
        user_name="system-owner",
        status="1",
        roles=[SimpleNamespace(tenant_id=0, role_code="R_SUPER", status="1")],
    )
    at_response = []

    async def observed_app(scope, receive, send):
        async def observe(message):
            if message["type"] == "http.response.start":
                at_response.append((message["status"], list(events)))
            await send(message)

        await test_app(scope, receive, observe)

    async with AsyncClient(
        transport=ASGITransport(app=observed_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/platform/tenants/7001/{action}",
            headers={
                "X-Platform-Reason": "qualification",
                "X-Platform-Ticket": "CI-1",
                "X-Correlation-ID": "ci-1",
            },
        )
    if failure is None:
        assert at_response == [(200, ["business", "audit", "commit"])]
        db.commit.assert_awaited_once()
    else:
        assert response.status_code == (503 if failure == "audit" else 500)
        assert "commit" not in events
        assert db.rollback.await_count >= 1
