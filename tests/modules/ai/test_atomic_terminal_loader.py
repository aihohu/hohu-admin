"""The live waiter must honor the same completion proof as HTTP confirmation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.gateway.atomic_execution import (
    PreparedExecutionOutcomeUnknown,
)
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import atomic_completion_service
from tests.modules.ai.test_atomic_prepared_completion import approve, count_real_handler
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)


@pytest.mark.parametrize("corruption", ["success_log", "failed_owner", "failed_error"])
async def test_waiter_rejects_corrupted_completion_proof(
    completion_case, monkeypatch, corruption
):
    case = completion_case
    failed = corruption.startswith("failed")
    count_real_handler(case, monkeypatch, return_failure=failed)
    assert (await approve(case)).data.status == ("failed" if failed else "succeeded")
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
        await executor._load_prepared_terminal_result(
            case.confirmation_id, tenant=case.tenant
        )
    assert len(case.calls) == 1


@pytest.mark.parametrize("failed", [False, True])
async def test_waiter_reads_valid_terminal_without_reexecution(
    completion_case, monkeypatch, failed
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=failed)
    await approve(case)
    result, duration = await executor._load_prepared_terminal_result(
        case.confirmation_id, tenant=case.tenant
    )
    assert result.ok is not failed
    assert duration >= 0
    assert len(case.calls) == 1


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
