"""Commit-stage control flow and concurrency, not real COMMIT ACK-loss evidence."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.authorization_owner import _QUARANTINED
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai.agents.gateway import authorization_execution
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.api import confirm as confirm_module
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service import authorization_lifecycle_candidate
from app.modules.ai.service.authorization_chat_candidate import chat_candidate
from app.modules.system.service.authorization_ai_tool_adapter import (
    system_ai_tool_adapter,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    approve,
    assert_success,
    count_real_handler,
    observe,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_real_commit_ack import (
    clear_action_write,
    observe_action_write,
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

    def before(session):
        status = session.connection().info.get("test_action_commit_status")
        session.info["test_commit_status"] = status
        if boundary == "before_commit":
            inject(status)

    def after(session):
        if boundary == "after_commit":
            inject(session.info.get("test_commit_status"))

    def inject(status):
        if status == target_status and not injected:
            injected.append(status)
            raise RuntimeError("Injected transaction-stage boundary exception")

    event.listen(engine.sync_engine, "after_cursor_execute", observe_action_write)
    event.listen(engine.sync_engine, "begin", clear_action_write)
    event.listen(Session, "before_commit", before)
    event.listen(Session, "after_commit", after)
    try:
        response = await approve(case)
    finally:
        event.remove(Session, "after_commit", after)
        event.remove(Session, "before_commit", before)
        event.remove(engine.sync_engine, "begin", clear_action_write)
        event.remove(engine.sync_engine, "after_cursor_execute", observe_action_write)
    assert injected == [target_status]
    assert response.data.status == "running"
    state = await observe(case)
    expected = (
        ("pending_confirmation" if stage == "claim" else "running")
        if boundary == "before_commit"
        else target_status
    )
    assert state["action_status"] == expected
    assert len(case.calls) == (0 if stage == "claim" else 1)
    if expected == "succeeded":
        assert_success(case, state)
    else:
        assert state["user_ids"] == []
    if expected in {"succeeded", "failed"}:
        assert (await approve(case)).data.status == expected
    elif expected == "running":
        # A separate request can fence only after the original lease expires.
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                update(AiPreparedAction)
                .where(AiPreparedAction.action_id == case.action_id)
                .values(
                    execution_lease_expires_at=AiPreparedAction.approved_at
                    + timedelta(microseconds=1)
                )
            )
        assert (await approve(case)).data.status == "failed"
        assert (await observe(case))["user_ids"] == []
    assert len(case.calls) == (0 if stage == "claim" else 1)


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
    original_commit = authorization_execution.commit_authorization_session
    injected, committed = [], []

    async def capture_commit(db):
        await original_commit(db)
        committed.append(db)

    async def fail_after_close(db):
        await real_close(db)
        if db in committed and not injected:
            injected.append(True)
            raise error_type("Injected cleanup failure after committed success")

    monkeypatch.setattr(
        authorization_execution, "commit_authorization_session", capture_commit
    )
    monkeypatch.setattr(AsyncSession, "close", fail_after_close)
    assert (await approve(case)).data.status == "succeeded"
    assert injected == [True]
    assert_success(case, await observe(case))


async def test_confirmed_failure_projection_can_be_repaired_without_execution(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=True)
    real_finalize = chat_candidate.recover

    async def fail_projection(*args, **kwargs):
        await real_finalize(*args, **kwargs)
        raise RuntimeError("Injected failed-result projection error")

    monkeypatch.setattr(chat_candidate, "recover", fail_projection)
    assert (await approve(case)).data.status == "failed"
    assert (await observe(case))["history"] == []
    monkeypatch.setattr(chat_candidate, "recover", real_finalize)
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
    real_execute = authorization_execution.PlannedGatewayOwner.execute_claim

    async def corrupt_after_commit(self, claim, **kwargs):
        result = await real_execute(self, claim, **kwargs)
        async with AsyncSessionLocal() as db, db.begin():
            log = await db.get(AiOperationLog, case.log_id)
            log.args_hash = "f" * 64
        return result

    monkeypatch.setattr(
        authorization_execution.PlannedGatewayOwner,
        "execute_claim",
        corrupt_after_commit,
    )
    # Original ACK stays known; a later independent request cannot trust
    # the subsequently corrupted completion binding or disclose its body.
    assert (await approve(case)).data.status == "succeeded"
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
    real_execute = authorization_execution.PlannedGatewayOwner.execute_claim

    async def fence_before_entry(self, claim, **kwargs):
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
        return await real_execute(self, claim, **kwargs)

    monkeypatch.setattr(
        authorization_execution.PlannedGatewayOwner, "execute_claim", fence_before_entry
    )
    response = await approve(case)
    assert response.data.status == "running"
    assert case.calls == []
    state = await observe(case)
    assert state["user_ids"] == []
    assert state["action_status"] == state["operation_status"] == "running"


async def test_insufficient_entry_lease_rejects_before_business(
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
        authorization_lifecycle_candidate,
        "_EXECUTION_LEASE_TTL",
        timedelta(milliseconds=350),
    )

    async def unexpected_heartbeat(*_args, **_kwargs):
        raise AssertionError(
            "Atomic execution cannot start an independent PA heartbeat"
        )

    monkeypatch.setattr(
        confirm_module, "_keep_execution_lease_alive", unexpected_heartbeat
    )
    response = await approve(case)
    assert response.data.status == "failed"
    assert case.calls == []
    assert (await observe(case))["user_ids"] == []


async def test_cancellation_resistant_close_keeps_known_ack_and_quarantine(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    original_commit = authorization_execution.commit_authorization_session
    original_close = AsyncSession.close
    committed, stalled = [], []
    release = asyncio.Event()

    async def capture_commit(db):
        await original_commit(db)
        committed.append(db)

    async def resistant_close(db):
        await original_close(db)
        if db in committed and not stalled:
            stalled.append(db)
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

    monkeypatch.setattr(
        authorization_execution, "commit_authorization_session", capture_commit
    )
    monkeypatch.setattr(AsyncSession, "close", resistant_close)
    try:
        response = await asyncio.wait_for(approve(case), 3)
        assert response.data.status == "succeeded"
        assert len(stalled) == 1
        assert any(row[0] is stalled[0] for row in _QUARANTINED.values())
        assert_success(case, await observe(case))
    finally:
        release.set()
        tasks = [row[2] for row in _QUARANTINED.values() if row[0] in stalled]
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


async def test_two_prepared_approvals_cannot_claim_the_same_action(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    prepare = system_ai_tool_adapter.prepare_input
    execute = authorization_execution.PlannedGatewayOwner.execute_claim
    both_ready, execution_entered, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    prepared, executing = [], []

    async def held_input(*args, **kwargs):
        value = await prepare(*args, **kwargs)
        prepared.append(True)
        if len(prepared) == 2:
            both_ready.set()
        await both_ready.wait()
        return value

    async def held_execution(self, claim, **kwargs):
        executing.append(claim)
        execution_entered.set()
        await release.wait()
        return await execute(self, claim, **kwargs)

    monkeypatch.setattr(system_ai_tool_adapter, "prepare_input", held_input)
    monkeypatch.setattr(
        authorization_execution.PlannedGatewayOwner, "execute_claim", held_execution
    )
    requests = [asyncio.create_task(approve(case)) for _ in range(2)]
    try:
        await asyncio.wait_for(execution_entered.wait(), 5)
        completed, _ = await asyncio.wait(
            requests, timeout=3, return_when=asyncio.FIRST_COMPLETED
        )
        assert len(completed) == 1
        assert next(iter(completed)).result().data.status == "running"
        assert len(executing) == 1
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(*requests), 10)
        assert {response.data.status for response in responses} == {
            "running",
            "succeeded",
        }
        assert len(case.calls) == 1
        assert_success(case, await observe(case))
    finally:
        release.set()
        both_ready.set()
        await asyncio.gather(*requests, return_exceptions=True)
