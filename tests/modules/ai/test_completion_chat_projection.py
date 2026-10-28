"""Late stream snapshots cannot replace durable prepared completion facts."""

import pytest
from sqlalchemy import select

from app.core.exceptions import BusinessRuleException
from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.gateway.atomic_execution import OUTCOME_UNKNOWN_MESSAGE
from app.modules.ai.api.chat import _finalize_stream_turn
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.chat_run_service import chat_run_finalizer
from tests.modules.ai.test_atomic_prepared_completion import approve, count_real_handler
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_completion_resume_guards import leave_claim_running


async def _message(db, case):
    return await db.scalar(
        select(AiMessage).where(
            AiMessage.tenant_id == case.tenant.tenant_id,
            AiMessage.conversation_id == case.conversation_id,
            AiMessage.role == "assistant",
        )
    )


async def _seed_partial_message(case):
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        message = AiMessage(
            tenant_id=case.tenant.tenant_id,
            conversation_id=case.conversation_id,
            parent_message_id=action.source_user_message_id,
            role="assistant",
            content="I am checking the requested change.",
            trace_id=action.trace_id,
            agent_code=action.agent_code,
            is_active=True,
            tool_calls=[
                {
                    "tool": action.execute_tool_name,
                    "tool_call_id": action.execute_tool_call_id,
                }
            ],
        )
        db.add(message)


async def test_prepared_finalizer_rejects_unverified_failed_fast_path(
    completion_case, monkeypatch
):
    case = completion_case
    await leave_claim_running(case, monkeypatch)
    async with AsyncSessionLocal() as db:
        action = await db.get(AiPreparedAction, case.action_id)
        action.status = "failed"
        action.execution_owner = None
        with pytest.raises(BusinessRuleException, match="锁定"):
            await chat_run_finalizer.finalize_prepared_action(
                db,
                action=action,
                ok=False,
                error_code="AI_INTERNAL_ERROR",
                error_msg="legacy failure",
                tenant=case.tenant,
            )


@pytest.mark.parametrize("incoming", ["failure", "pending"])
async def test_late_stream_preserves_current_success_with_stale_orm(
    completion_case, monkeypatch, incoming
):
    case = completion_case
    await _seed_partial_message(case)
    count_real_handler(case, monkeypatch)
    async with AsyncSessionLocal() as stale_db:
        stale_message = await _message(stale_db, case)
        assert "ok" not in stale_message.tool_calls[0]
        action = await stale_db.get(AiPreparedAction, case.action_id)
        assert (await approve(case)).data.status == "succeeded"
        await chat_run_finalizer.finalize_assistant_turn(
            stale_db,
            conversation_id=case.conversation_id,
            trace_id=action.trace_id,
            source_user_message_id=action.source_user_message_id,
            content="",
            tool_calls=[
                {
                    "tool": action.execute_tool_name,
                    "tool_call_id": case.tool_call_id,
                    "ok": False if incoming == "failure" else None,
                    "result": None,
                    "error_code": "AI_INTERNAL_ERROR"
                    if incoming == "failure"
                    else None,
                    "duration_ms": 0 if incoming == "failure" else None,
                }
            ],
            agent_code=action.agent_code,
            tenant=case.tenant,
        )
        await stale_db.commit()
    async with AsyncSessionLocal() as db:
        message = await _message(db, case)
        card = next(
            x for x in message.tool_calls if x["tool_call_id"] == case.tool_call_id
        )
        assert card["ok"] is True
        assert card["result"] is not None
        assert card.get("error_code") is None
    assert len(case.calls) == 1


async def test_unknown_notice_appends_to_nonempty_message_once(
    completion_case, monkeypatch
):
    case = completion_case
    await _seed_partial_message(case)
    await leave_claim_running(case, monkeypatch)
    for _ in range(2):
        async with AsyncSessionLocal() as db:
            action = await db.get(AiPreparedAction, case.action_id)
            await _finalize_stream_turn(
                db,
                conversation_id=case.conversation_id,
                trace_id=action.trace_id,
                source_user_message_id=action.source_user_message_id,
                content="",
                tool_calls=None,
                agent_code=action.agent_code,
                stream_error_code="AI_EXECUTION_OUTCOME_UNKNOWN",
                tenant=case.tenant,
                actor_user_version=1,
            )
    async with AsyncSessionLocal() as db:
        message = await _message(db, case)
        assert "I am checking" in message.content
        assert message.content.count(OUTCOME_UNKNOWN_MESSAGE) == 1
        assert not any(
            key in message.tool_calls[0]
            for key in ("ok", "error_code", "duration_ms", "result")
        )


async def test_late_failure_card_cannot_terminalize_running_projection(
    completion_case, monkeypatch
):
    case = completion_case
    await _seed_partial_message(case)
    await leave_claim_running(case, monkeypatch)
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        await chat_run_finalizer.finalize_assistant_turn(
            db,
            conversation_id=case.conversation_id,
            trace_id=action.trace_id,
            source_user_message_id=action.source_user_message_id,
            content="",
            tool_calls=[
                {
                    "tool": action.execute_tool_name,
                    "tool_call_id": action.execute_tool_call_id,
                    "ok": False,
                    "error_code": "AI_INTERNAL_ERROR",
                    "duration_ms": 0,
                }
            ],
            agent_code=action.agent_code,
            tenant=case.tenant,
        )
    async with AsyncSessionLocal() as db:
        message = await _message(db, case)
        assert OUTCOME_UNKNOWN_MESSAGE in message.content
        assert not any(
            key in message.tool_calls[0] for key in ("ok", "error_code", "duration_ms")
        )
        assert (await db.get(AiPreparedAction, case.action_id)).status == "running"


@pytest.mark.parametrize(
    ("binding", "status"),
    [
        ("legacy", "running"),
        ("legacy", "failed"),
        ("legacy", "rejected"),
        ("legacy", "expired"),
        ("orphan_write", "running"),
        ("orphan_readonly", "running"),
        ("orphan_write", "success"),
        ("orphan_write", "failed"),
        ("orphan_write", "rejected"),
        ("orphan_write", "expired"),
    ],
)
async def test_late_stream_preserves_unverified_write_log(
    completion_case, monkeypatch, binding, status
):
    case = completion_case
    await _seed_partial_message(case)
    await leave_claim_running(case, monkeypatch)
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        trace_id = action.trace_id
        source_id = action.source_user_message_id
        if binding == "legacy":
            action.execution_owner = "legacy-random-token"
            action.status = status
        else:
            await db.delete(action)
        if binding == "orphan_readonly":
            (await db.get(AiOperationLog, case.log_id)).readonly_snapshot = True
        (await db.get(AiOperationLog, case.log_id)).status = status
    async with AsyncSessionLocal() as db, db.begin():
        await chat_run_finalizer.finalize_assistant_turn(
            db,
            conversation_id=case.conversation_id,
            trace_id=trace_id,
            source_user_message_id=source_id,
            content="",
            tool_calls=[
                {
                    "tool": "user.create",
                    "tool_call_id": case.tool_call_id,
                    "ok": False,
                    "error_code": "AI_INTERNAL_ERROR",
                    "duration_ms": 0,
                }
            ],
            agent_code="user_mgmt",
            tenant=case.tenant,
        )
    async with AsyncSessionLocal() as db:
        message = await _message(db, case)
        if binding == "orphan_readonly":
            assert message.tool_calls[0]["ok"] is False
            assert OUTCOME_UNKNOWN_MESSAGE not in message.content
        else:
            assert OUTCOME_UNKNOWN_MESSAGE in message.content
            assert "ok" not in message.tool_calls[0]
        assert (await db.get(AiOperationLog, case.log_id)).status == status
