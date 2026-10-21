"""Atomic completion invariants; every database write uses outer rollback."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from tenant_helpers import tenant_context

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.gateway.result import ResultProjection, ToolResult
from app.modules.ai.agents.hitl.constants import PreparedActionStatus
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service import atomic_completion_service as completion_module
from app.modules.ai.service.atomic_completion_service import (
    ClaimBinding,
    atomic_completion_service,
    has_protocol_marker,
    is_protocol_owner,
    new_execution_owner,
)
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.system.models.user import User


@pytest.fixture
async def atomic_case(db_session):
    user = await db_session.scalar(
        select(User).where(User.tenant_id == 0, User.user_name == "admin")
    )
    assert user is not None
    tenant = tenant_context(tenant_id=0, actor_user_id=user.user_id)
    token = uuid4().hex
    conversation = AiConversation(tenant_id=0, user_id=user.user_id, title=token)
    db_session.add(conversation)
    await db_session.flush()
    source = AiMessage(
        tenant_id=0,
        conversation_id=conversation.conversation_id,
        role="user",
        content="Create user",
        trace_id=token,
        is_active=True,
    )
    db_session.add(source)
    await db_session.flush()
    now = datetime.now(UTC)
    args = {"name": "atomic test"}
    snapshot = {"business": {"version": 1}}
    action = AiPreparedAction(
        confirmation_id=f"cid_{token}",
        status="running",
        row_version=3,
        interaction_flow="direct",
        requested_outcome="direct",
        approval_mode="required",
        dispatch_mode="execute_after_approval",
        execute_tool_call_id=f"tc_{token}",
        execute_tool_name="user.create",
        frozen_args=args,
        args_hash=compute_args_hash(args),
        snapshot=snapshot,
        snapshot_hash=compute_args_hash(snapshot),
        presentation={},
        user_id=user.user_id,
        tenant_id=0,
        conversation_id=conversation.conversation_id,
        source_user_message_id=source.message_id,
        trace_id=token,
        agent_code="user_mgmt",
        tool_codes=["user.create"],
        expires_at=now + timedelta(minutes=5),
        approved_by=user.user_id,
        approved_at=now,
        execution_owner=new_execution_owner("user.create"),
        execution_lease_expires_at=now + timedelta(minutes=1),
    )
    log = AiOperationLog(
        tenant_id=0,
        user_id=user.user_id,
        conversation_id=conversation.conversation_id,
        source_user_message_id=source.message_id,
        agent_code="user_mgmt",
        trace_id=token,
        tool_name="user.create",
        tool_call_id=action.execute_tool_call_id,
        args_hash=action.args_hash,
        args_summary="tool=user.create",
        risk_level="high",
        execution_mode="hitl",
        status="running",
        confirmation_id=action.confirmation_id,
        approved_by=user.user_id,
        readonly_snapshot=False,
        queued_at=now.replace(tzinfo=None),
        started_at=now.replace(tzinfo=None),
    )
    db_session.add_all([action, log])
    await db_session.flush()
    claim = ClaimBinding.from_action(action, log.log_id)
    deps = SimpleNamespace(
        tenant=tenant, user=user, agent=SimpleNamespace(code="user_mgmt")
    )
    return SimpleNamespace(
        action=action,
        log=log,
        source=source,
        conversation=conversation,
        tenant=tenant,
        claim=claim,
        deps=deps,
    )


@pytest.mark.parametrize("tool", ["user.create", "user.export", "user.import_preview"])
def test_owner_is_canonical_and_bound_to_explicit_tool_kind(tool):
    owner = new_execution_owner(tool)
    assert len(owner) <= 64
    assert is_protocol_owner(owner, tool)
    assert not is_protocol_owner(owner.upper(), tool)
    assert not is_protocol_owner(owner, "unknown.write")
    other_kind = "user.export" if tool == "user.create" else "user.create"
    assert not is_protocol_owner(owner, other_kind)


def test_unlisted_tool_cannot_get_an_owner():
    with pytest.raises(BusinessRuleException):
        new_execution_owner("unknown.write")


def test_malformed_protocol_marker_is_not_a_legacy_failure_proof():
    assert has_protocol_marker("v0162-db:malformed")
    assert has_protocol_marker("v0162-file:malformed")
    assert not is_protocol_owner("v0162-db:malformed", "user.create")
    assert not has_protocol_marker(None)
    assert not has_protocol_marker("legacy-owner")


async def test_claim_is_frozen_and_requires_running_and_log_identity(atomic_case):
    case = atomic_case
    with pytest.raises(FrozenInstanceError):
        case.claim.row_version = 4
    with pytest.raises(BusinessRuleException):
        ClaimBinding.from_action(case.action, 0)
    case.action.status = "failed"
    with pytest.raises(BusinessRuleException):
        ClaimBinding.from_action(case.action, case.log.log_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", 123),
        ("conversation_id", 123),
        ("source_user_message_id", None),
        ("trace_id", "wrong"),
        ("agent_code", None),
        ("confirmation_id", None),
        ("args_hash", "0" * 64),
        ("tool_name", "user.update"),
        ("status", "pending_confirmation"),
        ("approved_by", None),
        ("readonly_snapshot", True),
    ],
)
async def test_required_log_binding_and_running_are_strict(
    db_session, atomic_case, field, value
):
    case = atomic_case
    setattr(case.log, field, value)
    await db_session.flush()
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_claim(
            db_session, case.claim, tenant=case.tenant
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", 987),
        ("row_version", 4),
        ("execution_owner", "v0162-db:00000000-0000-0000-0000-000000000001"),
        ("log_id", 123),
        ("snapshot_hash", "0" * 64),
    ],
)
async def test_claim_mismatch_does_not_grant_execution(
    db_session, atomic_case, field, value
):
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_claim(
            db_session,
            replace(atomic_case.claim, **{field: value}),
            tenant=atomic_case.tenant,
        )


async def test_hashes_are_recomputed_from_frozen_payload(db_session, atomic_case):
    case = atomic_case
    case.action.frozen_args = {"changed": True}
    await db_session.flush()
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_claim(
            db_session, case.claim, tenant=case.tenant
        )


async def test_expired_claim_cannot_enter_but_can_be_failed_after_rollback(
    db_session, atomic_case
):
    case = atomic_case
    case.action.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_claim(
            db_session, case.claim, tenant=case.tenant
        )
    locked = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant, require_running=False
    )
    terminal = await atomic_completion_service.complete_failed(
        db_session,
        locked,
        tenant=case.tenant,
        error_code="TEST_ROLLBACK_CONFIRMED",
        duration_ms=4,
    )
    assert terminal.status == "failed"
    assert terminal.execution_owner == case.claim.execution_owner
    assert terminal.execution_lease_expires_at is None
    assert case.log.status == "failed"
    assert case.log.finished_at is not None
    assert not await prepared_action_service.renew_execution_lease(
        db_session,
        action_id=terminal.action_id,
        execution_owner=case.claim.execution_owner,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        tenant=case.tenant,
    )


async def test_success_writes_required_pair_and_lineage_without_commit(
    db_session, atomic_case, monkeypatch
):
    case = atomic_case

    async def forbidden_commit():
        pytest.fail("Service cannot commit")

    monkeypatch.setattr(db_session, "commit", forbidden_commit)
    locked = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant
    )
    # A transaction that entered in time may finish after its lease expires.
    case.action.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    result = ToolResult.success(
        {"created": 1},
        projection=ResultProjection(subject_refs=({"type": "user", "id": "123"},)),
    )
    terminal = await atomic_completion_service.complete_success(
        db_session,
        locked,
        result=result,
        deps=case.deps,
        duration_ms=5,
        tenant=case.tenant,
    )
    assert terminal.status == "succeeded"
    assert terminal.row_version == case.claim.row_version + 1
    assert terminal.execution_owner is None
    assert terminal.result_data == {"created": 1}
    assert terminal.subject_refs == [{"type": "user", "id": "123"}]
    assert case.log.status == "success"
    assert case.log.target_summary == '[{"id":"123","type":"user"}]'
    readback = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant, require_running=False
    )
    assert readback.action.status == "succeeded"
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.complete_success(
            db_session,
            readback,
            result=result,
            deps=case.deps,
            duration_ms=5,
            tenant=case.tenant,
        )


async def test_exact_cas_is_required_even_after_lock(db_session, atomic_case):
    case = atomic_case
    locked = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant
    )
    await db_session.execute(
        update(AiPreparedAction)
        .where(AiPreparedAction.action_id == case.claim.action_id)
        .values(row_version=4)
        .execution_options(synchronize_session=False)
    )
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.complete_success(
            db_session,
            locked,
            result=ToolResult.success({}),
            deps=case.deps,
            duration_ms=5,
            tenant=case.tenant,
        )


async def test_file_failure_has_no_db_only_proof(db_session, atomic_case):
    case = atomic_case
    case.action.execute_tool_name = case.log.tool_name = "user.export"
    case.action.execution_owner = new_execution_owner("user.export")
    await db_session.flush()
    claim = ClaimBinding.from_action(case.action, case.log.log_id)
    locked = await atomic_completion_service.lock_claim(
        db_session, claim, tenant=case.tenant, require_running=False
    )
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.complete_failed(
            db_session,
            locked,
            tenant=case.tenant,
            error_code="TEST_ROLLBACK_CONFIRMED",
            duration_ms=4,
        )
    assert case.action.status == case.log.status == "running"


async def test_legacy_transition_cannot_terminalize_new_owner(db_session, atomic_case):
    case = atomic_case
    result = await prepared_action_service.transition_status(
        db_session,
        action_id=case.claim.action_id,
        expected_status=PreparedActionStatus.RUNNING,
        expected_version=3,
        target_status=PreparedActionStatus.FAILED,
        tenant=case.tenant,
    )
    assert result is None
    assert case.action.status == "running"


async def test_delete_refuses_new_running_even_after_lease_expiry(
    db_session, atomic_case
):
    case = atomic_case
    case.action.execution_lease_expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.flush()
    with pytest.raises(BusinessRuleException):
        await prepared_action_service.expire_for_conversation_delete(
            db_session,
            conversation_id=case.conversation.conversation_id,
            user_id=case.tenant.actor_user_id,
            tenant=case.tenant,
        )
    assert case.action.status == case.log.status == "running"


async def test_claim_locks_have_a_transaction_local_timeout(db_session, atomic_case):
    await atomic_completion_service.lock_claim(
        db_session, atomic_case.claim, tenant=atomic_case.tenant
    )
    assert await db_session.scalar(select(func.current_setting("lock_timeout"))) == "2s"


async def test_missing_required_log_prevents_execution(db_session, atomic_case):
    await db_session.execute(
        delete(AiOperationLog).where(AiOperationLog.log_id == atomic_case.log.log_id)
    )
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_claim(
            db_session, atomic_case.claim, tenant=atomic_case.tenant
        )


async def test_pending_log_is_validated_before_claim(db_session, atomic_case):
    case = atomic_case
    case.action.status = case.log.status = "pending_confirmation"
    case.action.approved_at = case.action.approved_by = None
    case.log.approved_by = case.log.started_at = None
    await db_session.flush()
    assert (
        await atomic_completion_service.lock_action_log(
            db_session,
            case.action,
            tenant=case.tenant,
            expected_status="pending_confirmation",
        )
        is case.log
    )


async def test_inactive_source_is_refreshed_before_entry(db_session, atomic_case):
    case = atomic_case
    await db_session.execute(
        update(AiMessage)
        .where(AiMessage.message_id == case.source.message_id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    assert case.source.is_active is True
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_claim(
            db_session, case.claim, tenant=case.tenant
        )
    locked = await atomic_completion_service.lock_claim(
        db_session,
        case.claim,
        tenant=case.tenant,
        require_active=False,
        require_running=False,
    )
    assert locked.action.status == "running"


async def test_failure_result_cannot_be_completed_as_success(db_session, atomic_case):
    case = atomic_case
    locked = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant
    )
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.complete_success(
            db_session,
            locked,
            result=ToolResult.failure("TEST_ERROR", "Failure"),
            deps=case.deps,
            duration_ms=2,
            tenant=case.tenant,
        )
    assert case.action.status == case.log.status == "running"


async def test_scope_lineage_uses_refreshed_transaction_user(
    db_session, atomic_case, monkeypatch
):
    case = atomic_case
    locked = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant
    )
    current_user = object()
    calls = []

    async def load_current(db, *, tenant):
        assert db is db_session and tenant is case.tenant
        calls.append("reload")
        return current_user

    async def scope_hash(db, user):
        assert db is db_session and user is current_user
        calls.append("scope")
        return "post-write-scope-hash"

    monkeypatch.setattr(completion_module, "load_live_user_authority", load_current)
    monkeypatch.setattr(
        completion_module.result_projection_service,
        "compute_data_scope_hash",
        scope_hash,
    )
    terminal = await atomic_completion_service.complete_success(
        db_session,
        locked,
        result=ToolResult.success({}, projection=ResultProjection(scope_bound=True)),
        deps=case.deps,
        duration_ms=2,
        tenant=case.tenant,
    )
    assert calls == ["reload", "scope"]
    assert terminal.data_scope_hash == "post-write-scope-hash"


async def test_required_log_exact_cas_rejects_post_lock_changes(
    db_session, atomic_case
):
    case = atomic_case
    locked = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant
    )
    await db_session.execute(
        update(AiOperationLog)
        .where(AiOperationLog.log_id == case.log.log_id)
        .values(status="failed")
        .execution_options(synchronize_session=False)
    )
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.complete_success(
            db_session,
            locked,
            result=ToolResult.success({}),
            deps=case.deps,
            duration_ms=2,
            tenant=case.tenant,
        )


async def test_retained_failed_owner_supports_strict_readback(db_session, atomic_case):
    case = atomic_case
    locked = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant, require_running=False
    )
    await atomic_completion_service.complete_failed(
        db_session,
        locked,
        error_code="TEST_CONFIRMED_FAILURE",
        duration_ms=1,
        tenant=case.tenant,
    )
    readback = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant, require_running=False
    )
    assert readback.action.status == "failed"
    case.log.error_code = "ANOTHER_FAILURE"
    await db_session.flush()
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_claim(
            db_session, case.claim, tenant=case.tenant, require_running=False
        )


async def test_failed_pair_without_error_is_not_a_credible_terminal(
    db_session, atomic_case
):
    case = atomic_case
    locked = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant, require_running=False
    )
    await atomic_completion_service.complete_failed(
        db_session,
        locked,
        error_code="TEST_CONFIRMED_FAILURE",
        duration_ms=1,
        tenant=case.tenant,
    )
    case.action.error_code = case.log.error_code = None
    await db_session.flush()
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_claim(
            db_session, case.claim, tenant=case.tenant, require_running=False
        )


async def _complete_for_terminal_read(db_session, case, status):
    locked = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant
    )
    if status == "failed":
        return await atomic_completion_service.complete_failed(
            db_session,
            locked,
            error_code="TEST_CONFIRMED_FAILURE",
            duration_ms=2,
            tenant=case.tenant,
        )
    return await atomic_completion_service.complete_success(
        db_session,
        locked,
        result=ToolResult.success({"created": 1}),
        deps=case.deps,
        duration_ms=2,
        tenant=case.tenant,
    )


@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_terminal_read_does_not_manufacture_execution_claim(
    db_session, atomic_case, status
):
    case = atomic_case
    await _complete_for_terminal_read(db_session, case, status)
    completion = await atomic_completion_service.lock_terminal(
        db_session, case.action.confirmation_id, tenant=case.tenant
    )
    assert completion.action is case.action
    assert completion.log is case.log
    assert not hasattr(completion, "claim")
    assert completion.action.status == status


@pytest.mark.parametrize(
    "target,field,value",
    [
        ("action", "execution_owner", "legacy-owner"),
        ("action", "execution_owner", "v0162-db:malformed"),
        ("action", "finished_at", None),
        ("action", "execution_lease_expires_at", datetime.now(UTC)),
        ("action", "frozen_args", {"changed": True}),
        ("action", "snapshot", {"changed": True}),
        ("action", "approved_by", None),
        ("source", "trace_id", "another-trace"),
        ("source", "role", "assistant"),
        ("log", "approved_by", None),
    ],
)
async def test_terminal_read_rejects_incomplete_references(
    db_session, atomic_case, target, field, value
):
    case = atomic_case
    await _complete_for_terminal_read(db_session, case, "failed")
    setattr(getattr(case, target), field, value)
    await db_session.flush()
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_terminal(
            db_session, case.action.confirmation_id, tenant=case.tenant
        )


async def test_terminal_read_rejects_missing_failure_error(db_session, atomic_case):
    case = atomic_case
    await _complete_for_terminal_read(db_session, case, "failed")
    case.action.error_code = case.log.error_code = None
    await db_session.flush()
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_terminal(
            db_session, case.action.confirmation_id, tenant=case.tenant
        )


@pytest.mark.parametrize("status", ["pending_confirmation", "running"])
async def test_terminal_reader_does_not_treat_nonterminal_as_a_result(
    db_session, atomic_case, status
):
    case = atomic_case
    case.action.status = case.log.status = status
    await db_session.flush()
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_terminal(
            db_session, case.action.confirmation_id, tenant=case.tenant
        )


async def test_fact_only_lock_cannot_authorize_success(db_session, atomic_case):
    case = atomic_case
    case.action.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    locked = await atomic_completion_service.lock_claim(
        db_session,
        case.claim,
        tenant=case.tenant,
        require_active=False,
        require_running=False,
    )
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.complete_success(
            db_session,
            locked,
            result=ToolResult.success({}),
            deps=case.deps,
            duration_ms=2,
            tenant=case.tenant,
        )
    assert case.action.status == case.log.status == "running"


@pytest.mark.parametrize(
    "target_status", [PreparedActionStatus.SUCCEEDED, PreparedActionStatus.FAILED]
)
async def test_legacy_terminal_cas_cannot_clear_reserved_owner(
    db_session, atomic_case, target_status
):
    case = atomic_case
    case.action.execution_owner = "v0162-db:malformed"
    await db_session.flush()
    result = await prepared_action_service.transition_status(
        db_session,
        action_id=case.action.action_id,
        expected_status=PreparedActionStatus.RUNNING,
        expected_version=case.action.row_version,
        target_status=target_status,
        tenant=case.tenant,
    )
    assert result is None
    assert case.action.execution_owner == "v0162-db:malformed"
    assert case.action.status == "running"


@pytest.mark.parametrize(
    "owner",
    [
        "legacy-owner",
        "v0162-db:malformed",
        "v0162-file:00000000-0000-0000-0000-000000000001",
    ],
)
async def test_failed_owner_retention_requires_db_protocol(
    db_session, atomic_case, owner
):
    case = atomic_case
    with pytest.raises(BusinessRuleException):
        await prepared_action_service.transition_status(
            db_session,
            action_id=case.action.action_id,
            expected_status=PreparedActionStatus.RUNNING,
            expected_version=case.action.row_version,
            expected_execution_owner=owner,
            retain_failed_owner=True,
            target_status=PreparedActionStatus.FAILED,
            tenant=case.tenant,
        )
    assert case.action.status == "running"


async def test_owner_changed_after_lock_cannot_complete_old_claim(
    db_session, atomic_case
):
    case = atomic_case
    locked = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant
    )
    await db_session.execute(
        update(AiPreparedAction)
        .where(AiPreparedAction.action_id == case.action.action_id)
        .values(execution_owner=new_execution_owner("user.create"))
        .execution_options(synchronize_session=False)
    )
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.complete_success(
            db_session,
            locked,
            result=ToolResult.success({}),
            deps=case.deps,
            duration_ms=1,
            tenant=case.tenant,
        )
    assert case.log.status == "running"


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_owner", "legacy-owner"),
        ("error_code", "TEST_CONFLICT"),
    ],
)
async def test_success_terminal_rejects_conflicting_owner_or_error(
    db_session, atomic_case, field, value
):
    case = atomic_case
    await _complete_for_terminal_read(db_session, case, "succeeded")
    setattr(case.action, field, value)
    if field == "error_code":
        case.log.error_code = value
    await db_session.flush()
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_terminal(
            db_session, case.action.confirmation_id, tenant=case.tenant
        )


async def test_terminal_reader_requires_original_actor(db_session, atomic_case):
    case = atomic_case
    await _complete_for_terminal_read(db_session, case, "succeeded")
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_terminal(
            db_session,
            case.action.confirmation_id,
            tenant=replace(case.tenant, actor_user_id=case.tenant.actor_user_id + 1),
        )
