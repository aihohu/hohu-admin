"""Atomic platform business/audit transaction boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.core.exceptions import BusinessException
from app.db import session as session_module
from app.modules.platform import audit as platform_audit


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
