"""AI operation-log owner 与审计分支权限测试。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.constants import STATUS_ENABLED
from app.core.exceptions import NotFoundException
from app.core.tenant import TenantContext
from app.modules.ai.api.operation_log import get_operation_log


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
        "app.modules.ai.api.operation_log.operation_log_service.get_by_tool_call_id",
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
        "errorCode": "AI_RESULT_PROJECTION_FORBIDDEN",
        "finishedAt": datetime(2026, 8, 14, 8, 0, 1, tzinfo=UTC),
    }


async def test_trace_auditor_without_chat_permission_keeps_audit_dto() -> None:
    user = _user("ai:trace:view")
    with patch(
        "app.modules.ai.api.operation_log.operation_log_service.get_by_tool_call_id",
        AsyncMock(return_value=_log()),
    ):
        result = await get_operation_log(
            tool_call_id="tc_status",
            db=MagicMock(),
            current_user=user,
            tenant=user._tenant_context,
        )

    assert result.data.tool_name == "user.update"
    assert result.data.duration_ms == 1000


async def test_owner_with_chat_permission_still_needs_projection_lineage() -> None:
    user = _user("ai:chat:use")
    with (
        patch(
            "app.modules.ai.api.operation_log.operation_log_service.get_by_tool_call_id",
            AsyncMock(return_value=_log()),
        ),
        patch(
            "app.modules.ai.api.operation_log.result_projection_service.lineage_for_operation_log",
            AsyncMock(return_value=None),
        ),
    ):
        result = await get_operation_log(
            tool_call_id="tc_status",
            db=MagicMock(),
            current_user=user,
            tenant=user._tenant_context,
        )

    assert result.data.error_code == "AI_RESULT_PROJECTION_FORBIDDEN"


async def test_operation_log_lookup_is_scoped_to_authenticated_tenant() -> None:
    lookup = AsyncMock(return_value=None)
    user = _user("ai:trace:view")
    with patch(
        "app.modules.ai.api.operation_log.operation_log_service.get_by_tool_call_id",
        lookup,
    ):
        with pytest.raises(NotFoundException):
            await get_operation_log(
                tool_call_id="tc_status",
                db=MagicMock(),
                current_user=user,
                tenant=user._tenant_context,
            )

    assert lookup.await_args.kwargs["tenant"].tenant_id == 0
