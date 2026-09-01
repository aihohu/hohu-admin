"""Database and authorization tests for Phase 4 AI Trace projections."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.constants import STATUS_DISABLED, STATUS_ENABLED
from app.core.exceptions import AuthorizationException, NotFoundException
from app.core.tenant import TenantContext
from app.modules.ai.agents.hitl.constants import AiExecutionMode, AiOperationStatus
from app.modules.ai.api.operation_log import _ensure_trace_view, list_traces
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.schemas.operation_log import TraceListQuery
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.trace_service import trace_service
from app.modules.system.models.user import User


def _user(*permissions: str, role_code: str = "R_AUDITOR", status=STATUS_ENABLED):
    return SimpleNamespace(
        user_id=7001,
        user_name="auditor",
        roles=[
            SimpleNamespace(
                role_code=role_code,
                status=status,
                menus=[SimpleNamespace(permission=value) for value in permissions],
            )
        ],
        _tenant_context=TenantContext(
            tenant_id=0,
            tenant_code="default",
            actor_user_id=7001,
            tenant_version=1,
            source="access_token",
        ),
    )


async def _start(
    db,
    *,
    trace_id: str,
    tool_call_id: str,
    user_id: int,
    tenant_id: int = 0,
    agent_code: str | None = "user_mgmt",
    tool_name: str = "user.lookup",
    source_user_message_id: int | None = None,
) -> AiOperationLog:
    log_id = await operation_log_service.start_operation(
        db,
        trace_id=trace_id,
        conversation_id=81001,
        tenant_id=tenant_id,
        source_user_message_id=source_user_message_id,
        agent_code=agent_code,
        user_id=user_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        args_hash="a" * 64,
        args_summary="rawArgs=TRACE_SENTINEL_SECRET",
        risk_level="low",
        execution_mode=AiExecutionMode.AUTONOMOUS.value,
        status=AiOperationStatus.RUNNING,
    )
    return await db.get(AiOperationLog, log_id)


async def test_trace_list_is_grouped_filtered_and_stably_paginated(db_session) -> None:
    actor_id = int(
        (
            await db_session.execute(
                select(User.user_id).where(User.user_name == "admin")
            )
        ).scalar_one()
    )
    now = datetime(2026, 8, 24, 8, 0)
    first = await _start(
        db_session,
        trace_id="tr_test_phase4_first",
        tool_call_id="tc_phase4_first_1",
        user_id=actor_id,
        tool_name="phase4.trace",
    )
    first.queued_at = now
    second = await _start(
        db_session,
        trace_id="tr_test_phase4_first",
        tool_call_id="tc_phase4_first_2",
        user_id=actor_id,
        agent_code=None,
        tool_name="phase4.trace",
    )
    second.queued_at = now + timedelta(seconds=1)
    latest = await _start(
        db_session,
        trace_id="tr_test_phase4_latest",
        tool_call_id="tc_phase4_latest",
        user_id=actor_id,
        agent_code="dept_mgmt",
        tool_name="phase4.trace",
    )
    latest.queued_at = now + timedelta(seconds=2)
    await _start(
        db_session,
        trace_id="tr_test_phase4_other_tenant",
        tool_call_id="tc_phase4_other_tenant",
        user_id=actor_id,
        tenant_id=9,
        tool_name="phase4.trace",
    )
    await db_session.flush()

    page = await trace_service.list_traces(
        db_session,
        tenant_id=0,
        query=TraceListQuery(current=1, size=1, toolName="phase4.trace"),
    )
    filtered = await trace_service.list_traces(
        db_session,
        tenant_id=0,
        query=TraceListQuery(agentCode="user_mgmt", toolName="phase4.trace"),
    )

    assert page.total == 2
    assert [record.trace_id for record in page.records] == ["tr_test_phase4_latest"]
    assert filtered.total == 1
    assert filtered.records[0].trace_id == "tr_test_phase4_first"
    assert filtered.records[0].agent_codes == ["unknown", "user_mgmt"]
    assert filtered.records[0].operation_count == 2


async def test_trace_detail_omits_message_content_and_raw_audit_fields(
    db_session,
) -> None:
    actor = (
        await db_session.execute(select(User).where(User.user_name == "admin"))
    ).scalar_one()
    conversation = AiConversation(
        conversation_id=81001,
        user_id=actor.user_id,
        title="Phase 4 Trace",
        model_name="1",
    )
    message = AiMessage(
        message_id=81002,
        conversation_id=conversation.conversation_id,
        role="user",
        message_type="text",
        content="TRACE_SENTINEL_MESSAGE_CONTENT",
    )
    db_session.add_all([conversation, message])
    await db_session.flush()
    operation = await _start(
        db_session,
        trace_id="tr_test_phase4_detail",
        tool_call_id="tc_phase4_detail",
        user_id=actor.user_id,
        source_user_message_id=message.message_id,
    )
    await operation_log_service.attach_target_summary(
        db_session,
        operation.log_id,
        [{"type": "user", "id": str(actor.user_id), "email": "secret@example.com"}],
    )
    await db_session.flush()

    detail = await trace_service.get_trace(
        db_session,
        tenant_id=0,
        trace_id="tr_test_phase4_detail",
    )
    serialized = detail.model_dump_json(by_alias=True)

    assert detail.operations[0].source_message_role == "user"
    assert detail.operations[0].target_summary[0].id == str(actor.user_id)
    assert "TRACE_SENTINEL_MESSAGE_CONTENT" not in serialized
    assert "TRACE_SENTINEL_SECRET" not in serialized
    assert "secret@example.com" not in serialized
    assert "argsSummary" not in serialized


async def test_trace_detail_cross_tenant_matches_not_found(db_session) -> None:
    actor_id = int(
        (
            await db_session.execute(
                select(User.user_id).where(User.user_name == "admin")
            )
        ).scalar_one()
    )
    await _start(
        db_session,
        trace_id="tr_test_phase4_hidden",
        tool_call_id="tc_phase4_hidden",
        user_id=actor_id,
        tenant_id=7,
    )

    with pytest.raises(NotFoundException) as exc_info:
        await trace_service.get_trace(
            db_session,
            tenant_id=8,
            trace_id="tr_test_phase4_hidden",
        )

    assert exc_info.value.error_code == "AI_TRACE_NOT_FOUND"


def test_trace_permission_accepts_auditor_and_enabled_super_only() -> None:
    _ensure_trace_view(_user("ai:trace:view"))
    _ensure_trace_view(_user(role_code="R_SUPER"))

    with pytest.raises(AuthorizationException) as missing:
        _ensure_trace_view(_user())
    assert missing.value.error_code == "AI_TRACE_FORBIDDEN"

    with pytest.raises(AuthorizationException) as disabled_super:
        _ensure_trace_view(_user(role_code="R_SUPER", status=STATUS_DISABLED))
    assert disabled_super.value.error_code == "AI_TRACE_FORBIDDEN"


async def test_trace_list_api_uses_authenticated_tenant() -> None:
    page = MagicMock()
    lookup = AsyncMock(return_value=page)
    with patch(
        "app.modules.ai.api.operation_log.trace_service.list_traces",
        lookup,
    ):
        response = await list_traces(
            query=TraceListQuery(),
            db=MagicMock(),
            current_user=_user("ai:trace:view"),
        )

    assert response.data is page
    assert lookup.await_args.kwargs["tenant_id"] == 0
