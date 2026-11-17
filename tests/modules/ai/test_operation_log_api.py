"""AI operation-log owner 与审计分支权限测试。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.constants import STATUS_ENABLED
from app.core.exceptions import NotFoundException
from app.core.tenant import TenantContext
from app.modules.ai.service.completion_read_state_service import EffectiveCompletion
from app.modules.ai.service.operation_status_projection import (
    project_operation as get_operation_log,
)


@pytest.fixture(autouse=True)
def verified_completion_snapshot(monkeypatch):
    # These unit tests isolate authorization; real PA/log proof is covered by
    # test_completion_read_projection against committed PostgreSQL records.
    log = _log()
    monkeypatch.setattr(
        "app.modules.ai.service.operation_status_projection.completion_read_state_service.read",
        AsyncMock(
            return_value={
                log.tool_call_id: EffectiveCompletion(
                    log.status,
                    log.error_code,
                    log.finished_at,
                    log.duration_ms,
                    verified=True,
                )
            }
        ),
    )


def _user(*permissions: str):
    role = SimpleNamespace(
        role_code="R_USER",
        status=STATUS_ENABLED,
        menus=[SimpleNamespace(permission=value) for value in permissions],
    )
    return SimpleNamespace(
        user_id=100,
        user_name="alice",
        roles=[role],
        _tenant_context=TenantContext(
            tenant_id=0,
            tenant_code="default",
            actor_user_id=100,
            tenant_version=1,
            source="access_token",
        ),
    )


def _log():
    return SimpleNamespace(
        tenant_id=0,
        user_id=100,
        tool_call_id="tc_status",
        tool_name="user.update",
        status="success",
        error_code=None,
        started_at=datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 14, 8, 0, 1, tzinfo=UTC),
        duration_ms=1000,
    )


async def test_owner_without_chat_permission_receives_minimal_status_only() -> None:
    user = _user()
    with patch(
        "app.modules.ai.service.operation_status_projection.operation_log_service.get_by_tool_call_id",
        AsyncMock(return_value=_log()),
    ):
        result = await get_operation_log(
            tool_call_id="tc_status",
            db=MagicMock(),
            current_user=user,
            tenant=user._tenant_context,
        )

    assert result.data.model_dump(by_alias=True) == {
        "toolCallId": "tc_status",
        "status": "success",
        "errorCode": None,
        "finishedAt": None,
    }


async def test_auditor_without_a_field_owner_receives_only_completion_status() -> None:
    user = _user("ai:trace:view")
    with patch(
        "app.modules.ai.service.operation_status_projection.operation_log_service.get_by_tool_call_id",
        AsyncMock(return_value=_log()),
    ):
        result = await get_operation_log(
            tool_call_id="tc_status",
            db=MagicMock(),
            current_user=user,
            tenant=user._tenant_context,
        )

    assert result.data.status == "success"
    assert not hasattr(result.data, "tool_name")
    assert not hasattr(result.data, "duration_ms")


async def test_chat_permission_alone_cannot_unlock_audit_metadata() -> None:
    user = _user("ai:chat:use")
    with (
        patch(
            "app.modules.ai.service.operation_status_projection.operation_log_service.get_by_tool_call_id",
            AsyncMock(return_value=_log()),
        ),
    ):
        result = await get_operation_log(
            tool_call_id="tc_status",
            db=MagicMock(),
            current_user=user,
            tenant=user._tenant_context,
        )

    assert result.data.status == "success"
    assert not hasattr(result.data, "tool_name")


async def test_operation_log_lookup_is_scoped_to_authenticated_tenant() -> None:
    lookup = AsyncMock(return_value=None)
    user = _user("ai:trace:view")
    missing_action = AsyncMock(return_value=None)
    with (
        patch(
            "app.modules.ai.service.operation_status_projection.operation_log_service.get_by_tool_call_id",
            lookup,
        ),
        patch(
            "app.modules.ai.service.operation_status_projection.prepared_action_service.get_by_execute_tool_call_id",
            missing_action,
        ),
    ):
        with pytest.raises(NotFoundException):
            await get_operation_log(
                tool_call_id="tc_status",
                db=MagicMock(),
                current_user=user,
                tenant=user._tenant_context,
            )

    assert lookup.await_args.kwargs["tenant"].tenant_id == 0
    assert missing_action.await_args.kwargs["tenant"].tenant_id == 0
