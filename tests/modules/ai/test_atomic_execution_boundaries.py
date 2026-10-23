"""Commit-stage control flow and concurrency, not real COMMIT ACK-loss evidence."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.api import confirm as confirm_module
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.chat_run_service import chat_run_finalizer
from tests.modules.ai.test_atomic_prepared_completion import (
    approve,
    assert_success,
    count_real_handler,
    observe,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)


@pytest.mark.parametrize("stage", ["claim", "execution", "failure"])
@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
async def test_commit_boundary_exception_never_invents_business_failure(
    completion_case, monkeypatch, stage, boundary
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=stage == "failure")
    target_status = {"claim": "running", "execution": "succeeded", "failure": "failed"}[
        stage
    ]
    injected = []

    def fail_at_boundary(session):
        matches = [
            row
            for row in session.identity_map.values()
            if isinstance(row, AiPreparedAction)
            and row.action_id == case.action_id
            and row.status == target_status
        ]
        if matches and not injected:
            injected.append(target_status)
            raise RuntimeError("Injected transaction-stage boundary exception")

    event.listen(Session, boundary, fail_at_boundary)
    try:
        response = await approve(case)
    finally:
        event.remove(Session, boundary, fail_at_boundary)
    assert injected == [target_status]
    state = await observe(case)
    if stage == "claim":
        assert case.calls == []
        assert state["user_ids"] == []
        assert response.data.status == (
            "running" if boundary == "before_commit" else "failed"
        )
        assert state["action_status"] == (
            "pending_confirmation" if boundary == "before_commit" else "failed"
        )
    elif boundary == "after_commit" and stage == "execution":
        assert response.data.status == "succeeded"
        assert_success(case, state)
    elif boundary == "after_commit":
        assert response.data.status == "failed"
        assert state["user_ids"] == []
        assert state["action_status"] == state["operation_status"] == "failed"
        async with AsyncSessionLocal() as db:
            action = await db.get(AiPreparedAction, case.action_id)
            assert action.execution_owner.startswith("v0162-db:")
            assert action.execution_lease_expires_at is None
    else:
        # Stage B locks and fences the original DB owner after the uncertain
        # session is discarded; durable absence is now a proved rollback.
        assert response.data.status == "failed"
        assert state["user_ids"] == []
        assert state["action_status"] == state["operation_status"] == "failed"
    if stage != "claim":
        assert len(case.calls) == 1


async def test_two_approvals_commit_one_business_action(completion_case, monkeypatch):
    case = completion_case
    count_real_handler(case, monkeypatch)
    responses = await asyncio.gather(approve(case), approve(case))
    assert {response.data.status for response in responses} <= {"running", "succeeded"}
    assert "succeeded" in {response.data.status for response in responses}
    assert_success(case, await observe(case))


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_session_close_after_committed_success_is_auxiliary(
    completion_case, monkeypatch, error_type
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    real_close = AsyncSession.close
    injected = []

    async def fail_after_close(db):
        success_present = any(
            isinstance(row, AiPreparedAction)
            and row.action_id == case.action_id
            and row.status == "succeeded"
            for row in db.identity_map.values()
        )
        await real_close(db)
        if success_present and not injected:
            injected.append(True)
            raise error_type("Injected cleanup failure after committed success")

    monkeypatch.setattr(AsyncSession, "close", fail_after_close)
    assert (await approve(case)).data.status == "succeeded"
    assert injected == [True]
    assert_success(case, await observe(case))


async def test_confirmed_failure_projection_can_be_repaired_without_execution(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=True)
    real_finalize = chat_run_finalizer.finalize_prepared_action

    async def fail_projection(*args, **kwargs):
        await real_finalize(*args, **kwargs)
        raise RuntimeError("Injected failed-result projection error")

    monkeypatch.setattr(chat_run_finalizer, "finalize_prepared_action", fail_projection)
    assert (await approve(case)).data.status == "failed"
    assert (await observe(case))["history"] == []
    monkeypatch.setattr(chat_run_finalizer, "finalize_prepared_action", real_finalize)
    assert (await approve(case)).data.status == "failed"
    state = await observe(case)
    assert len(case.calls) == 1
    assert state["user_ids"] == []
    assert state["action_status"] == state["operation_status"] == "failed"
    assert len(state["history"]) == 1


async def test_corrupted_completion_binding_stays_unknown_without_overwrite(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    real_execute = confirm_module.execute_approved_prepared_action

    async def corrupt_after_commit(claim, deps):
        result = await real_execute(claim, deps)
        async with AsyncSessionLocal() as db, db.begin():
            log = await db.get(AiOperationLog, case.log_id)
            log.args_hash = "f" * 64
        return result

    monkeypatch.setattr(
        confirm_module, "execute_approved_prepared_action", corrupt_after_commit
    )
    assert (await approve(case)).data.status == "running"
    assert (await approve(case)).data.status == "running"
    state = await observe(case)
    assert state["action_status"] == "succeeded"
    assert state["operation_status"] == "success"
    assert len(state["user_ids"]) == len(case.calls) == 1
    assert state["history"] == []


@pytest.mark.parametrize("corruption", ["owner", "error_code"])
async def test_corrupted_failed_proof_cannot_bypass_terminal_validation(
    completion_case, monkeypatch, corruption
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=True)
    assert (await approve(case)).data.status == "failed"
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        if corruption == "owner":
            action.execution_owner = "v0162-db:invalid"
        else:
            action.error_code = None
            log = await db.get(AiOperationLog, case.log_id)
            log.error_code = None
    response = await approve(case)
    assert response.data.status == "running"
    assert "请勿重新办理" in response.msg
    assert len(case.calls) == 1


async def test_confirm_context_lock_timeout_is_bounded_and_executes_nothing(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    async with AsyncSessionLocal() as holder, holder.begin():
        await holder.execute(
            select(AiConversation)
            .where(
                AiConversation.tenant_id == case.tenant.tenant_id,
                AiConversation.conversation_id == case.conversation_id,
            )
            .with_for_update()
        )
        async with asyncio.timeout(8):
            response = await approve(case)
        assert response.data.status == "running"
        assert "请勿重新办理" in response.msg
        assert case.calls == []
    assert (await approve(case)).data.status == "succeeded"
    assert_success(case, await observe(case))
    assert len((await observe(case))["history"]) == 1


async def test_running_owner_is_fenced_before_handler_entry(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    real_execute = confirm_module.execute_approved_prepared_action

    async def fence_before_entry(claim, deps):
        async with AsyncSessionLocal() as db, db.begin():
            action = await db.scalar(
                select(AiPreparedAction)
                .where(
                    AiPreparedAction.action_id == case.action_id,
                    AiPreparedAction.tenant_id == case.tenant.tenant_id,
                )
                .with_for_update()
            )
            action.row_version += 1
        return await real_execute(claim, deps)

    monkeypatch.setattr(
        confirm_module, "execute_approved_prepared_action", fence_before_entry
    )
    response = await approve(case)
    assert response.data.status == "running"
    assert case.calls == []
    state = await observe(case)
    assert state["user_ids"] == []
    assert state["action_status"] == state["operation_status"] == "running"


async def test_lease_expiry_while_holding_transaction_does_not_invalidate_success(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    registered = ToolRegistry.get().find("user.create")
    real_tool = registered.fn

    async def hold_past_entry_lease(ctx, **args):
        result = await real_tool(ctx, **args)
        await asyncio.sleep(0.5)
        return result

    monkeypatch.setattr(registered, "fn", hold_past_entry_lease)
    monkeypatch.setattr(
        confirm_module, "_EXECUTION_LEASE_TTL", timedelta(milliseconds=350)
    )

    async def unexpected_heartbeat(*_args, **_kwargs):
        raise AssertionError(
            "Atomic execution cannot start an independent PA heartbeat"
        )

    monkeypatch.setattr(
        confirm_module, "_keep_execution_lease_alive", unexpected_heartbeat
    )
    response = await approve(case)
    assert response.data.status == "succeeded"
    assert_success(case, await observe(case))
