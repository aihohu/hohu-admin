"""The live waiter must honor the same completion proof as HTTP confirmation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import BusinessException
from app.db.authorization_chat_owner import ChatOwner
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.session import engine
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.gateway.atomic_execution import (
    PreparedExecutionOutcomeUnknown,
)
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import atomic_completion_service
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.authorization_lifecycle_candidate import (
    ResumeBinding,
    lifecycle_candidate,
)
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import (
    approve,
    authenticated_actor,
    count_real_handler,
    observe,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)


async def recover_display(case):
    assert (
        await AuthorizationRuntime(engine).chat.recover(
            tenant=case.tenant, confirmation_id=case.confirmation_id
        )
        is not None
    )


@pytest.fixture(params=["settled", "late"])
def display_schedule(request, completion_case, monkeypatch):
    """Commit a real late display between reader discovery and lock recheck."""
    state = SimpleNamespace(armed=False, recovered=False, fired=False)
    first_projection = True
    reader_sessions = set()
    original_recover = ChatOwner.recover
    original_locked = lifecycle_candidate._locked
    original_discover = ai_context_candidate.discover

    async def recover(self, **kwargs):
        nonlocal first_projection
        if first_projection:
            first_projection = False
            if request.param == "late":
                # Exercise the endpoint's real optional-display timeout.
                await asyncio.Event().wait()
        result = await original_recover(self, **kwargs)
        state.recovered = True
        return result

    async def locked(db, *args, **kwargs):
        reader_sessions.add(db)
        try:
            return await original_locked(db, *args, **kwargs)
        finally:
            reader_sessions.remove(db)

    async def discover(db, *args, **kwargs):
        inventory = await original_discover(db, *args, **kwargs)
        if db in reader_sessions and state.armed and not state.recovered:
            state.armed = False
            await recover_display(completion_case)
            state.fired = True
        return inventory

    monkeypatch.setattr(ChatOwner, "recover", recover)
    monkeypatch.setattr(lifecycle_candidate, "_locked", locked)
    monkeypatch.setattr(ai_context_candidate, "discover", discover)
    return state


async def load_terminal(case):
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        action = await db.get(AiPreparedAction, case.action_id)
        binding = ResumeBinding(
            action.action_id,
            action.conversation_id,
            action.source_user_message_id,
            action.trace_id,
            action.execute_tool_call_id,
            action.execute_tool_name,
            action.agent_code,
            action.args_hash,
        )
    return await executor._load_prepared_terminal_result(
        case.confirmation_id,
        tenant=case.tenant,
        binding=binding,
        deps=SimpleNamespace(user=actor, tenant=case.tenant),
    )


@pytest.mark.parametrize("corruption", ["success_log", "failed_owner", "failed_error"])
async def test_waiter_rejects_corrupted_completion_proof(
    completion_case, monkeypatch, corruption, display_schedule
):
    case = completion_case
    failed = corruption.startswith("failed")
    count_real_handler(case, monkeypatch, return_failure=failed)
    assert (await approve(case)).data.status == ("failed" if failed else "succeeded")
    await recover_display(case)
    display_schedule.armed = True
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        assert action is not None and log is not None
        if corruption == "success_log":
            log.args_hash = "f" * 64
        elif corruption == "failed_owner":
            action.execution_owner = "v0162-db:malformed"
        else:
            action.error_code = log.error_code = None

    with pytest.raises(PreparedExecutionOutcomeUnknown):
        await load_terminal(case)
    assert len(case.calls) == 1
    assert not display_schedule.fired


@pytest.mark.parametrize("failed", [False, True])
async def test_waiter_reads_valid_terminal_without_reexecution(
    completion_case, monkeypatch, failed, display_schedule
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=failed)
    await approve(case)
    # Durable completion does not imply the optional display has committed.
    await recover_display(case)
    display_schedule.armed = True
    result, duration = await load_terminal(case)
    assert result.ok is not failed
    assert duration >= 0
    assert len(case.calls) == 1
    assert not display_schedule.fired


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("display_schedule", ["late"], indirect=True)
async def test_waiter_rejects_stale_display_inventory_then_reads_without_reexecution(
    completion_case, monkeypatch, failed, display_schedule
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=failed)
    assert (await approve(case)).data.status == ("failed" if failed else "succeeded")
    before = await observe(case)
    assert before["history"] == []
    assert not display_schedule.recovered
    display_schedule.armed = True

    with pytest.raises(PreparedExecutionOutcomeUnknown) as rejected:
        await load_terminal(case)
    assert isinstance(rejected.value.__cause__, BusinessException)
    assert rejected.value.__cause__.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
    assert display_schedule.fired and display_schedule.recovered

    after = await observe(case)
    assert len(after["history"]) == 1
    assert {key: value for key, value in after.items() if key != "history"} == {
        key: value for key, value in before.items() if key != "history"
    }
    result, duration = await load_terminal(case)
    assert result.ok is not failed
    assert duration >= 0
    assert len(case.calls) == 1
    assert await observe(case) == after


async def test_readonly_waiter_keeps_existing_result_path(monkeypatch):
    action = SimpleNamespace(
        status="succeeded",
        execute_tool_name="user.stats",
        execution_owner=None,
        result_data={"count": 1},
        result_ui=None,
        subject_refs=[],
        data_scope_hash=None,
        duration_ms=3,
    )
    monkeypatch.setattr(
        executor, "_get_prepared_action_by_confirmation", AsyncMock(return_value=action)
    )
    strict_read = AsyncMock(side_effect=AssertionError("Readonly has no write claim"))
    monkeypatch.setattr(atomic_completion_service, "lock_terminal", strict_read)
    result, duration = await executor._load_prepared_terminal_result(
        "readonly-confirmation", tenant=SimpleNamespace(tenant_id=0, actor_user_id=1)
    )
    assert result.ok and result.data == {"count": 1}
    assert duration == 3
    strict_read.assert_not_awaited()
