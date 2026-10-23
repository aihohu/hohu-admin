"""Confirmation and live waiters require a positive completion proof."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete

from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.gateway import atomic_execution, executor
from app.modules.ai.agents.gateway.atomic_execution import (
    PreparedExecutionOutcomeUnknown,
)
from app.modules.ai.api import confirm
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.confirm import ConfirmRequest
from app.modules.ai.service.atomic_completion_service import (
    atomic_completion_service,
    new_execution_owner,
)
from app.modules.auth.service import load_live_user_authority
from tests.modules.ai.test_atomic_prepared_completion import approve, count_real_handler
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)


@pytest.mark.parametrize("state", ["running", "succeeded"])
@pytest.mark.parametrize("context_change", ["soft_deleted", "source_missing"])
async def test_owned_started_confirmation_survives_context_loss(
    completion_case, monkeypatch, state, context_change
):
    case = completion_case
    if state == "running":
        monkeypatch.setattr(
            confirm,
            "execute_approved_prepared_action",
            AsyncMock(side_effect=PreparedExecutionOutcomeUnknown()),
        )
    else:
        count_real_handler(case, monkeypatch)
    assert (await approve(case)).data.status == state
    async with AsyncSessionLocal() as db, db.begin():
        if context_change == "soft_deleted":
            conversation = await db.get(AiConversation, case.conversation_id)
            conversation.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        else:
            action = await db.get(AiPreparedAction, case.action_id)
            await db.execute(
                delete(AiMessage).where(
                    AiMessage.message_id == action.source_user_message_id
                )
            )
    response = await approve(case)
    assert response.data.status == (
        "succeeded"
        if state == "succeeded" and context_change == "soft_deleted"
        else "running"
    )
    assert len(case.calls) == (1 if state == "succeeded" else 0)


@pytest.mark.parametrize("entrypoint", ["confirm", "waiter"])
async def test_file_failure_without_side_effect_proof_remains_unknown(
    completion_case, monkeypatch, entrypoint
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=True)
    assert (await approve(case)).data.status == "failed"
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        action.execute_tool_name = log.tool_name = "user.export"
        action.tool_codes = ["user.export"]
        action.execution_owner = new_execution_owner("user.export")
    async with AsyncSessionLocal() as db:
        # All database facts bind; this still proves no file disposition.
        await atomic_completion_service.lock_terminal(
            db, case.confirmation_id, tenant=case.tenant
        )
    if entrypoint == "confirm":
        assert (await approve(case)).data.status == "running"
    else:
        with pytest.raises(PreparedExecutionOutcomeUnknown):
            await executor._load_prepared_terminal_result(
                case.confirmation_id, tenant=case.tenant
            )
    assert len(case.calls) == 1


@pytest.mark.parametrize("state", ["running", "approved", "failed"])
async def test_legacy_write_waiter_cannot_infer_rollback(completion_case, state):
    case = completion_case
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        action.status = state
        action.execution_owner = "legacy-owner"
        log.status = "failed" if state == "failed" else "running"
    with pytest.raises(PreparedExecutionOutcomeUnknown):
        await executor._load_prepared_terminal_result(
            case.confirmation_id, tenant=case.tenant
        )
    assert case.calls == []


@pytest.mark.parametrize("blocked_stage", ["projection", "notification"])
async def test_optional_display_work_is_bounded_without_losing_success(
    completion_case, monkeypatch, blocked_stage
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    assert (await approve(case)).data.status == "succeeded"
    never = asyncio.Event()

    async def blocked(*_args, **_kwargs):
        await never.wait()

    if blocked_stage == "projection":
        monkeypatch.setattr(
            confirm.chat_run_finalizer, "finalize_prepared_action", blocked
        )
    else:
        monkeypatch.setattr(confirm, "_notify_prepared_terminal", blocked)
    async with AsyncSessionLocal() as db:
        reference = await db.get(AiPreparedAction, case.action_id)
        response = await asyncio.wait_for(
            confirm._project_prepared_completion(
                case.confirmation_id,
                current_user_id=case.tenant.actor_user_id,
                tenant=case.tenant,
                action_ref=reference,
            ),
            timeout=2,
        )
    assert response.data.status == "succeeded"
    assert len(case.calls) == 1


@pytest.mark.parametrize("state", ["rejected", "expired"])
@pytest.mark.parametrize("corruption", [None, "log_started", "version", "missing_log"])
@pytest.mark.parametrize("entrypoint", ["confirm", "waiter"])
async def test_duplicate_cancellation_requires_never_started_proof(
    completion_case, state, corruption, entrypoint
):
    case = completion_case
    if state == "expired":
        async with AsyncSessionLocal() as db, db.begin():
            action = await db.get(AiPreparedAction, case.action_id)
            action.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        response = await approve(case)
    else:
        async with AsyncSessionLocal() as db:
            actor = await load_live_user_authority(db, tenant=case.tenant)
            response = await confirm.confirm_tool(
                ConfirmRequest(confirmation_id=case.confirmation_id, action="reject"),
                db=db,
                current_user=actor,
            )
    assert response.data.status == state
    if corruption:
        async with AsyncSessionLocal() as db, db.begin():
            if corruption == "log_started":
                log = await db.get(AiOperationLog, case.log_id)
                log.started_at = datetime.now(UTC).replace(tzinfo=None)
            elif corruption == "missing_log":
                await db.execute(
                    delete(AiOperationLog).where(AiOperationLog.log_id == case.log_id)
                )
            else:
                action = await db.get(AiPreparedAction, case.action_id)
                action.row_version = 4
    if entrypoint == "confirm":
        response = await approve(case)
        assert response.data.status == ("running" if corruption else state)
    elif corruption:
        with pytest.raises(PreparedExecutionOutcomeUnknown):
            await executor._load_prepared_terminal_result(
                case.confirmation_id, tenant=case.tenant
            )
    else:
        result, _ = await executor._load_prepared_terminal_result(
            case.confirmation_id, tenant=case.tenant
        )
        assert result.ok is False
        assert result.error_code == (
            "USER_REJECTED" if state == "rejected" else "AI_HITL_EXPIRED"
        )
    assert case.calls == []


async def test_claim_commit_uncertainty_does_not_wait_for_invalidation(
    completion_case, monkeypatch
):
    case = completion_case
    release = asyncio.Event()
    db = AsyncSessionLocal()
    original_commit, original_invalidate = db.commit, db.invalidate
    actor = await load_live_user_authority(db, tenant=case.tenant)

    async def committed_but_unacknowledged():
        await original_commit()
        raise ConnectionError("Injected claim-stage acknowledgement error")

    async def stalled_invalidation():
        await release.wait()
        await original_invalidate()

    monkeypatch.setattr(db, "commit", committed_but_unacknowledged)
    monkeypatch.setattr(db, "invalidate", stalled_invalidation)
    try:
        started = time.monotonic()
        response = await asyncio.wait_for(
            confirm.confirm_tool(
                ConfirmRequest(confirmation_id=case.confirmation_id, action="approve"),
                db=db,
                current_user=actor,
            ),
            timeout=2,
        )
        assert time.monotonic() - started < 1.5
        assert response.data.status == "failed"
        assert case.calls == []
    finally:
        release.set()
        tasks = [
            task
            for session, task in atomic_execution._QUARANTINED_SESSIONS.values()
            if session is db
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        await db.close()
