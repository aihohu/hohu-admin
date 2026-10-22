"""Only fully bound, never-started cancellations can release deletion guards."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import BusinessRuleException
from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.hitl.constants import AiOperationStatus, PreparedActionStatus
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import (
    atomic_completion_service,
    new_execution_owner,
)
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.system.models.user import User
from tests.modules.system.test_user_delete_ai_dependencies import (
    delete_case as delete_case,
)
from tests.modules.system.test_user_delete_ai_dependencies import delete_target


async def cancelled_dependency(case, status):
    tenant = replace(case.tenant, actor_user_id=case.user_id)
    confirmation_id = f"cid_cancel_{case.marker}"
    async with AsyncSessionLocal() as db, db.begin():
        action = await prepared_action_service.create_pending(
            db,
            confirmation_id=confirmation_id,
            prepare_tool_call_id=None,
            prepare_tool_name=None,
            execute_tool_call_id=f"tc_cancel_{case.marker}",
            execute_tool_name="user.create",
            frozen_args={"user_name": "cancelled-fixture"},
            snapshot={"tool": "user.create"},
            snapshot_hash="",
            subject_ref=None,
            presentation={"title": "user.create"},
            interaction_flow="direct",
            requested_outcome="direct",
            user_id=case.user_id,
            tenant=tenant,
            conversation_id=case.conversation_id,
            source_user_message_id=case.source_id,
            trace_id=f"delete_{case.marker}",
            agent_code="user_mgmt",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            resolved_model_id=1,
            resolved_provider_id=1,
            require_live_source=True,
        )
        log_id = await operation_log_service.start_operation(
            db,
            trace_id=action.trace_id,
            conversation_id=case.conversation_id,
            tenant=tenant,
            user_id=case.user_id,
            source_user_message_id=case.source_id,
            readonly_snapshot=False,
            agent_code="user_mgmt",
            tool_name="user.create",
            tool_call_id=action.execute_tool_call_id,
            args_hash=action.args_hash,
            args_summary="Never-started cancellation",
            risk_level="high",
            execution_mode="hitl",
            status=AiOperationStatus.PENDING_CONFIRMATION,
            confirmation_id=confirmation_id,
        )
        case.owned_action_ids.append(action.action_id)
        case.owned_log_ids.append(log_id)
        rejected = status == "rejected"
        action = await prepared_action_service.transition_status(
            db,
            action_id=action.action_id,
            expected_status=PreparedActionStatus.PENDING_CONFIRMATION,
            expected_version=1,
            target_status=PreparedActionStatus(status),
            approved_by=case.user_id if rejected else None,
            error_code="USER_REJECTED" if rejected else "AI_HITL_EXPIRED",
            tenant=tenant,
        )
        assert action is not None
        if rejected:
            await operation_log_service.mark_rejected_if_pending(
                db,
                log_id,
                approved_by=case.user_id,
                tenant=tenant,
            )
        else:
            await operation_log_service.mark_expired_if_pending(
                db, log_id, tenant=tenant
            )
        return action.action_id, log_id


@pytest.mark.parametrize("status", ["rejected", "expired"])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("inactive", [False, True])
async def test_never_started_terminal_dependency_allows_normal_delete(
    delete_case, status, batch, inactive
):
    case = delete_case
    await cancelled_dependency(case, status)
    if inactive:
        async with AsyncSessionLocal() as db, db.begin():
            conversation = await db.get(AiConversation, case.conversation_id)
            source = await db.get(AiMessage, case.source_id)
            conversation.deleted_at = datetime.now(UTC)
            source.is_active = False
    error = await delete_target(case, batch=batch)
    assert error is None, error.error_code if error else None
    async with AsyncSessionLocal() as db:
        assert await db.get(User, case.user_id) is None


@pytest.mark.parametrize(
    "corruption",
    [
        "started",
        "version",
        "approval",
        "owner",
        "payload",
        "args_hash",
        "source",
        "missing_log",
        "finished",
        "log_tool",
        "legacy_annotation",
    ],
)
async def test_cancelled_label_without_never_started_proof_stays_blocked(
    delete_case, corruption
):
    case = delete_case
    action_id, log_id = await cancelled_dependency(case, "expired")
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, action_id)
        log = await db.get(AiOperationLog, log_id)
        if corruption == "started":
            log.started_at = datetime.now(UTC).replace(tzinfo=None)
        elif corruption == "version":
            action.row_version = 4
        elif corruption == "approval":
            action.approved_by = case.user_id
            action.approved_at = datetime.now(UTC)
        elif corruption == "owner":
            action.execution_owner = new_execution_owner("user.create")
        elif corruption == "payload":
            action.result_data = {"committed": True}
        elif corruption == "args_hash":
            log.args_hash = "f" * 64
        elif corruption == "source":
            source = await db.get(AiMessage, case.source_id)
            source.trace_id = "unrelated"
        elif corruption == "missing_log":
            await db.delete(log)
        elif corruption == "finished":
            action.finished_at = None
        elif corruption == "log_tool":
            log.tool_name = "user.update"
        else:
            log.result_summary = (
                '{"__v0162_reconciliation_v1":{"disposition":"unverified"}}'
            )
    error = await delete_target(case, batch=False)
    assert error is not None and error.error_code == "USER_AI_DEPENDENCY_UNRESOLVED"
    async with AsyncSessionLocal() as db:
        assert await db.get(User, case.user_id) is not None


@pytest.mark.parametrize("status", ["rejected", "expired"])
async def test_legacy_no_action_log_is_not_proof_of_never_started_execution(
    delete_case, status
):
    case = delete_case
    action_id, _log_id = await cancelled_dependency(case, status)
    async with AsyncSessionLocal() as db, db.begin():
        await db.delete(await db.get(AiPreparedAction, action_id))
    error = await delete_target(case, batch=False)
    assert error is not None and error.error_code == "USER_AI_DEPENDENCY_UNRESOLVED"


@pytest.mark.parametrize("status", ["rejected", "expired"])
@pytest.mark.parametrize("inactive", [False, True])
async def test_locked_unexecuted_read_preserves_bound_cancellation_fact(
    delete_case, status, inactive
):
    case = delete_case
    action_id, log_id = await cancelled_dependency(case, status)
    if inactive:
        async with AsyncSessionLocal() as db, db.begin():
            conversation = await db.get(AiConversation, case.conversation_id)
            source = await db.get(AiMessage, case.source_id)
            conversation.deleted_at = datetime.now(UTC)
            source.is_active = False
    async with AsyncSessionLocal() as db, db.begin():
        fact = await atomic_completion_service.lock_unexecuted_terminal(
            db,
            f"cid_cancel_{case.marker}",
            tenant=replace(case.tenant, actor_user_id=case.user_id),
        )
        assert fact.action.action_id == action_id
        assert fact.log.log_id == log_id
        assert fact.action.status == fact.log.status == status
        assert db.in_transaction()


@pytest.mark.parametrize("corruption", ["started", "missing_log", "wrong_actor"])
async def test_locked_unexecuted_read_rejects_unproven_cancellation(
    delete_case, corruption
):
    case = delete_case
    _action_id, log_id = await cancelled_dependency(case, "expired")
    tenant = replace(case.tenant, actor_user_id=case.user_id)
    async with AsyncSessionLocal() as db, db.begin():
        log = await db.get(AiOperationLog, log_id)
        if corruption == "started":
            log.started_at = datetime.now(UTC).replace(tzinfo=None)
        elif corruption == "missing_log":
            await db.delete(log)
        else:
            tenant = replace(tenant, actor_user_id=case.user_id + 1)
    async with AsyncSessionLocal() as db, db.begin():
        with pytest.raises(BusinessRuleException):
            await atomic_completion_service.lock_unexecuted_terminal(
                db, f"cid_cancel_{case.marker}", tenant=tenant
            )


async def test_unexecuted_validator_treats_missing_log_as_unknown(delete_case):
    case = delete_case
    action_id, _log_id = await cancelled_dependency(case, "expired")
    async with AsyncSessionLocal() as db:
        action = await db.get(AiPreparedAction, action_id)
        conversation = await db.get(AiConversation, case.conversation_id)
        source = await db.get(AiMessage, case.source_id)
        with pytest.raises(BusinessRuleException):
            atomic_completion_service.validate_locked_unexecuted_terminal(
                action,
                None,
                tenant=replace(case.tenant, actor_user_id=case.user_id),
                conversation=conversation,
                source=source,
            )
