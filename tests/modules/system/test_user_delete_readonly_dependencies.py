"""Registered readonly HITL facts must not permanently block user deletion."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.hitl.constants import AiOperationStatus, PreparedActionStatus
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.system.models.user import User
from tests.modules.system.test_user_delete_ai_dependencies import (
    delete_case as delete_case,
)
from tests.modules.system.test_user_delete_ai_dependencies import delete_target

READ_TOOL = "testreadonly.lookup"


@pytest.fixture(autouse=True)
def readonly_registry(monkeypatch):
    registry = ToolRegistry()

    async def read(*_args, **_kwargs):
        raise AssertionError("Deletion must never execute a tool")

    registry.register(
        AiToolMeta(
            name=READ_TOOL,
            agent="user_mgmt",
            summary="Read fixture",
            required_perms=(),
            readonly=True,
            risk="low",
            projection_kind="none",
        ),
        read,
    )
    monkeypatch.setattr(ToolRegistry, "_instance", registry)
    return registry


async def readonly_dependency(case, status):
    tenant = replace(case.tenant, actor_user_id=case.user_id)
    async with AsyncSessionLocal() as db, db.begin():
        action = await prepared_action_service.create_pending(
            db,
            confirmation_id=f"cid_read_{case.marker}",
            prepare_tool_call_id=None,
            prepare_tool_name=None,
            execute_tool_call_id=f"tc_read_{case.marker}",
            execute_tool_name=READ_TOOL,
            frozen_args={"query": "read fixture"},
            snapshot={"tool": READ_TOOL},
            snapshot_hash="",
            subject_ref=None,
            presentation={"title": READ_TOOL},
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
            projection_kind="none",
            require_live_source=True,
        )
        log_id = await operation_log_service.start_operation(
            db,
            trace_id=action.trace_id,
            conversation_id=case.conversation_id,
            source_user_message_id=case.source_id,
            readonly_snapshot=True,
            agent_code=action.agent_code,
            user_id=case.user_id,
            tenant=tenant,
            tool_name=READ_TOOL,
            tool_call_id=action.execute_tool_call_id,
            args_hash=action.args_hash,
            args_summary="read fixture",
            risk_level="low",
            execution_mode="hitl",
            status=AiOperationStatus.PENDING_CONFIRMATION,
            confirmation_id=action.confirmation_id,
        )
        case.owned_action_ids.append(action.action_id)
        case.owned_log_ids.append(log_id)
        if status in {"rejected", "expired"}:
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
            if rejected:
                await operation_log_service.mark_rejected_if_pending(
                    db, log_id, approved_by=case.user_id, tenant=tenant
                )
            else:
                await operation_log_service.mark_expired_if_pending(
                    db, log_id, tenant=tenant
                )
            return action.action_id, log_id
        action = await prepared_action_service.transition_status(
            db,
            action_id=action.action_id,
            expected_status=PreparedActionStatus.PENDING_CONFIRMATION,
            expected_version=1,
            target_status=PreparedActionStatus.APPROVED,
            approved_by=case.user_id,
            tenant=tenant,
        )
        action = await prepared_action_service.transition_status(
            db,
            action_id=action.action_id,
            expected_status=PreparedActionStatus.APPROVED,
            expected_version=2,
            target_status=PreparedActionStatus.RUNNING,
            execution_owner="legacy-read-owner",
            execution_lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            tenant=tenant,
        )
        await operation_log_service.mark_approved(
            db, log_id, approved_by=case.user_id, tenant=tenant
        )
        await operation_log_service.mark_running(db, log_id, tenant=tenant)
        action = await prepared_action_service.transition_status(
            db,
            action_id=action.action_id,
            expected_status=PreparedActionStatus.RUNNING,
            expected_version=3,
            target_status=PreparedActionStatus(status),
            result_data={"count": 1} if status == "succeeded" else None,
            error_code=None if status == "succeeded" else "READ_FAILED",
            duration_ms=1,
            tenant=tenant,
        )
        if status == "succeeded":
            await operation_log_service.mark_success(
                db, log_id, result_summary="count=1", duration_ms=1, tenant=tenant
            )
        else:
            await operation_log_service.mark_failed(
                db, log_id, error_code="READ_FAILED", duration_ms=1, tenant=tenant
            )
        return action.action_id, log_id


@pytest.mark.parametrize("status", ["succeeded", "failed", "rejected", "expired"])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("inactive", [False, True])
async def test_bound_registered_readonly_terminal_allows_user_delete(
    delete_case, status, batch, inactive
):
    case = delete_case
    await readonly_dependency(case, status)
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
        "unregistered",
        "registry_write",
        "reserved_db",
        "reserved_file",
        "marker",
        "log_readonly",
        "source",
        "log_actor",
        "log_tool",
        "log_status",
        "started",
        "finished",
        "args_hash",
        "snapshot_hash",
        "error",
        "missing_log",
    ],
)
async def test_readonly_claim_without_bound_registry_facts_still_blocks_delete(
    delete_case, readonly_registry, corruption
):
    case = delete_case
    action_id, log_id = await readonly_dependency(case, "succeeded")
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, action_id)
        log = await db.get(AiOperationLog, log_id)
        if corruption == "unregistered":
            readonly_registry._tools.clear()
        elif corruption == "registry_write":
            registered = readonly_registry.find(READ_TOOL)
            registered.meta = replace(registered.meta, readonly=False)
        elif corruption in {"reserved_db", "reserved_file"}:
            tool_name = "user.create" if corruption == "reserved_db" else "user.export"
            action.execute_tool_name = log.tool_name = tool_name
            registered = readonly_registry.find(READ_TOOL)
            readonly_registry.register(
                replace(registered.meta, name=tool_name), registered.fn
            )
        elif corruption == "marker":
            action.execution_owner = "v0162-db:malformed"
        elif corruption == "log_readonly":
            log.readonly_snapshot = False
        elif corruption == "source":
            source = await db.get(AiMessage, case.source_id)
            source.trace_id = "unrelated"
        elif corruption == "log_actor":
            log.user_id = case.tenant.actor_user_id
        elif corruption == "log_tool":
            log.tool_name = "user.create"
        elif corruption == "log_status":
            log.status = "failed"
        elif corruption == "started":
            log.started_at = None
        elif corruption == "finished":
            action.finished_at = None
        elif corruption == "args_hash":
            log.args_hash = "f" * 64
        elif corruption == "snapshot_hash":
            action.snapshot_hash = "f" * 64
        elif corruption == "error":
            log.error_code = "UNEXPECTED_ERROR"
        else:
            await db.delete(log)
    error = await delete_target(case, batch=False)
    assert error is not None and error.error_code == "USER_AI_DEPENDENCY_UNRESOLVED"
    async with AsyncSessionLocal() as db:
        assert await db.get(User, case.user_id) is not None


@pytest.mark.parametrize("status", ["rejected", "expired"])
@pytest.mark.parametrize("corruption", ["started", "version"])
async def test_readonly_cancellation_requires_never_started_bound_facts(
    delete_case, status, corruption
):
    case = delete_case
    action_id, log_id = await readonly_dependency(case, status)
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, action_id)
        log = await db.get(AiOperationLog, log_id)
        assert action.row_version == 2 and log.started_at is None
        if corruption == "started":
            log.started_at = datetime.now(UTC).replace(tzinfo=None)
        else:
            action.row_version = 4
    error = await delete_target(case, batch=False)
    assert error is not None and error.error_code == "USER_AI_DEPENDENCY_UNRESOLVED"
    async with AsyncSessionLocal() as db:
        assert await db.get(User, case.user_id) is not None
