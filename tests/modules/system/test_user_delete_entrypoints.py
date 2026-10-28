"""Entrypoint retries release whole transactions before invoking business once."""

from collections.abc import Callable

import pytest
from sqlalchemy import delete

from app.core.exceptions import BusinessRuleException
from app.db.base import user_roles
from app.db.session import AsyncSessionLocal
from app.modules.ai.api import confirm as confirm_module
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.confirm import ConfirmRequest
from app.modules.ai.service.user_delete_dependency_service import (
    UserDeleteDependencyChanged,
)
from app.modules.system.models.user import User
from app.modules.system.service.authorization_ai_delete_candidate import (
    ai_delete_candidate,
)
from tests.modules.ai.test_atomic_db_tools import (
    ToolCase,
    authenticated_actor,
)
from tests.modules.ai.test_atomic_db_tools import (
    db_tool_case as db_tool_case,
)


def inject_committed_dependency_changes(
    monkeypatch: pytest.MonkeyPatch,
    case: ToolCase,
    count: int,
    *,
    revoke_permission: bool = False,
    phase: str = "running",
) -> list[int]:
    """Insert actual FK children between context locks and final User locks."""
    transactions = []
    original = ai_delete_candidate.plan

    async def changed(db, tx, command, **kwargs):
        work = await original(db, tx, command, **kwargs)
        # Simulate a legacy writer that bypassed B while discovery was underway.
        async with AsyncSessionLocal() as writer, writer.begin():
            action = await writer.get(AiPreparedAction, case.action_id)
            if action.status != phase:
                return work
            transactions.append(id(db.sync_session.get_transaction()))
            for index in range(count):
                writer.add(
                    AiConversation(
                        tenant_id=case.tenant.tenant_id,
                        user_id=case.target_user_id,
                        title=f"Entrypoint dependency change {index}",
                    )
                )
            await writer.flush()
            if revoke_permission:
                await writer.execute(
                    delete(user_roles).where(
                        user_roles.c.tenant_id == case.tenant.tenant_id,
                        user_roles.c.user_id == case.tenant.actor_user_id,
                    )
                )
        return work

    monkeypatch.setattr(ai_delete_candidate, "plan", changed)
    return transactions


def count_real_delete_handler(
    monkeypatch: pytest.MonkeyPatch,
    case: ToolCase,
    *,
    after_handler: Callable[[], None] | None = None,
) -> None:
    original = ai_delete_candidate.apply

    async def counted(db, tx, command, work):
        result = await original(db, tx, command, work)
        assert result.ok, result.error_code
        case.calls.append((id(db), id(db.sync_session.get_transaction())))
        if after_handler is not None:
            after_handler()
        return result

    monkeypatch.setattr(ai_delete_candidate, "apply", counted)


async def approve(case: ToolCase):
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(db, case.tenant)
        return await confirm_module.confirm_tool(
            ConfirmRequest(confirmation_id=case.confirmation_id, action="approve"),
            db=db,
            current_user=actor,
        )


@pytest.mark.parametrize("db_tool_case", ["user.batch_delete"], indirect=True)
@pytest.mark.parametrize("changes", [1, 2])
async def test_changed_dependencies_abort_without_replanning_original_claim(
    db_tool_case: ToolCase, monkeypatch: pytest.MonkeyPatch, changes: int
) -> None:
    case = db_tool_case
    transactions = inject_committed_dependency_changes(monkeypatch, case, changes)
    count_real_delete_handler(monkeypatch, case)
    response = await approve(case)
    assert len(transactions) == 1
    async with AsyncSessionLocal() as observer:
        target = await observer.get(User, case.target_user_id)
        action = await observer.get(AiPreparedAction, case.action_id)
        log = await observer.get(AiOperationLog, case.log_id)
        assert target is not None
        assert response.data.status == action.status == log.status == "failed"
        assert (
            action.error_code
            == log.error_code
            == "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
        )
        assert action.execution_owner.startswith("v0162-db:")
        assert action.execution_lease_expires_at is None
        assert action.finished_at is not None and log.finished_at is not None
        assert case.calls == []
    await approve(case)
    assert case.calls == []
    assert len(transactions) == 1


@pytest.mark.parametrize("db_tool_case", ["user.batch_delete"], indirect=True)
async def test_changed_dependencies_before_claim_expire_without_execution(
    db_tool_case: ToolCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = db_tool_case
    transactions = inject_committed_dependency_changes(
        monkeypatch, case, 1, phase="pending_confirmation"
    )
    count_real_delete_handler(monkeypatch, case)
    with pytest.raises(BusinessRuleException) as error:
        await approve(case)
    assert error.value.error_code == "AI_PREPARED_ACTION_BINDING_INVALID"
    async with AsyncSessionLocal() as observer:
        assert await observer.get(User, case.target_user_id) is not None
        action = await observer.get(AiPreparedAction, case.action_id)
        log = await observer.get(AiOperationLog, case.log_id)
        assert action.status == log.status == "expired"
        assert action.execution_owner is None
        assert action.execution_lease_expires_at is None
        assert action.error_code == "AI_PREPARED_ACTION_BINDING_INVALID"
    assert (await approve(case)).data.status == "expired"
    assert len(transactions) == 1
    assert case.calls == []


@pytest.mark.parametrize("db_tool_case", ["user.batch_delete"], indirect=True)
async def test_dependency_error_after_handler_start_rolls_back_without_retry(
    db_tool_case: ToolCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = db_tool_case

    def fail_after_delete():
        raise UserDeleteDependencyChanged()

    count_real_delete_handler(monkeypatch, case, after_handler=fail_after_delete)
    response = await approve(case)
    assert len(case.calls) == 1
    async with AsyncSessionLocal() as observer:
        assert await observer.get(User, case.target_user_id) is not None
        assert (
            await observer.get(AiConversation, case.target_conversation_id) is not None
        )
        action = await observer.get(AiPreparedAction, case.action_id)
        log = await observer.get(AiOperationLog, case.log_id)
        assert response.data.status == action.status == log.status == "failed"
    await approve(case)
    assert len(case.calls) == 1
