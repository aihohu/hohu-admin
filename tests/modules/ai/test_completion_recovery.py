"""Recovery needs locked protocol facts; lease expiry alone is never proof."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from tenant_helpers import tenant_context

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.service.atomic_completion_service import (
    ClaimBinding,
    atomic_completion_service,
    new_execution_owner,
)
from tests.modules.ai.test_atomic_completion_service import atomic_case as atomic_case


async def reconcile(db, case, **kwargs):
    from app.modules.ai.service.completion_recovery_service import (  # noqa: PLC0415
        completion_recovery_service,
    )

    return await completion_recovery_service.reconcile(
        db, case.action.confirmation_id, tenant=case.tenant, **kwargs
    )


def facts(case):
    return (
        case.action.status,
        case.action.row_version,
        case.action.execution_owner,
        case.action.finished_at,
        case.action.execution_lease_expires_at,
        case.action.error_code,
        case.log.status,
        case.log.finished_at,
        case.log.error_code,
    )


@pytest.mark.parametrize("allow_fence", [False, True])
async def test_fresh_lease_without_original_claim_stays_unknown(
    db_session, atomic_case, allow_fence
):
    case = atomic_case
    before = facts(case)
    outcome = await reconcile(db_session, case, allow_fence=allow_fence)
    assert outcome.disposition == "unknown"
    assert not outcome.changed
    assert facts(case) == before


@pytest.mark.parametrize("expired", [False, True])
async def test_original_claim_with_explicit_unknown_commit_can_fence(
    db_session, atomic_case, expired, monkeypatch
):
    case = atomic_case
    if expired:
        case.action.execution_lease_expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
        await db_session.flush()
    no_commit = AsyncMock(side_effect=AssertionError("Service must not commit"))
    monkeypatch.setattr(db_session, "commit", no_commit)
    outcome = await reconcile(
        db_session, case, expected_claim=case.claim, allow_fence=True
    )
    assert outcome.disposition == "failed" and outcome.changed
    assert outcome.action.status == case.log.status == "failed"
    assert case.action.execution_owner == case.claim.execution_owner
    assert case.action.row_version == case.claim.row_version + 1
    assert case.action.execution_lease_expires_at is None
    assert case.action.finished_at is not None and case.log.finished_at is not None
    assert case.action.error_code == case.log.error_code
    assert case.action.error_code == "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
    no_commit.assert_not_awaited()
    with pytest.raises(BusinessRuleException):
        await atomic_completion_service.lock_claim(
            db_session, case.claim, tenant=case.tenant
        )


async def test_expired_recovery_reconstructs_claim_and_preserves_inactive_facts(
    db_session, atomic_case
):
    case = atomic_case
    case.action.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    case.conversation.deleted_at = datetime.now(UTC)
    case.source.is_active = False
    await db_session.flush()
    outcome = await reconcile(db_session, case)
    assert outcome.disposition == "failed" and outcome.changed
    assert case.action.execution_owner == case.claim.execution_owner
    assert case.action.row_version == case.claim.row_version + 1
    again = await reconcile(db_session, case)
    assert again.disposition == "failed" and not again.changed
    assert case.action.row_version == case.claim.row_version + 1


async def test_original_claim_does_not_implicitly_authorize_fresh_fence(
    db_session, atomic_case
):
    before = facts(atomic_case)
    outcome = await reconcile(db_session, atomic_case, expected_claim=atomic_case.claim)
    assert outcome.disposition == "unknown" and not outcome.changed
    assert facts(atomic_case) == before


@pytest.mark.parametrize(
    "corruption",
    [
        "legacy_owner",
        "malformed_owner",
        "wrong_tool_kind",
        "file",
        "missing_lease",
        "log_binding",
        "log_terminal",
        "frozen_hash",
        "source_binding",
    ],
)
async def test_absence_never_fails_untrusted_or_non_db_running(
    db_session, atomic_case, corruption
):
    case = atomic_case
    case.action.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if corruption == "legacy_owner":
        case.action.execution_owner = "old-process-owner"
    elif corruption == "malformed_owner":
        case.action.execution_owner = "v0162-db:not-a-uuid"
    elif corruption == "wrong_tool_kind":
        case.action.execution_owner = new_execution_owner("user.export")
    elif corruption == "file":
        case.action.execute_tool_name = case.log.tool_name = "user.export"
        case.action.execution_owner = new_execution_owner("user.export")
    elif corruption == "missing_lease":
        case.action.execution_lease_expires_at = None
    elif corruption == "log_binding":
        case.log.args_hash = "f" * 64
    elif corruption == "log_terminal":
        case.log.status = "success"
        case.log.finished_at = datetime.now(UTC).replace(tzinfo=None)
    elif corruption == "frozen_hash":
        case.action.frozen_args = {"tampered": True}
    else:
        case.source.trace_id = "wrong-source"
    await db_session.flush()
    before = facts(case)
    outcome = await reconcile(db_session, case, allow_fence=True)
    assert outcome.disposition == "unknown" and not outcome.changed
    assert facts(case) == before


@pytest.mark.parametrize("field,value", [("row_version", 2), ("log_id", 123)])
async def test_stale_original_claim_never_falls_back_to_current_owner(
    db_session, atomic_case, field, value
):
    case = atomic_case
    case.action.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()
    before = facts(case)
    outcome = await reconcile(
        db_session,
        case,
        expected_claim=replace(case.claim, **{field: value}),
        allow_fence=True,
    )
    assert outcome.disposition == "unknown" and not outcome.changed
    assert facts(case) == before


async def test_missing_log_is_unknown_and_does_not_change_action(
    db_session, atomic_case
):
    case = atomic_case
    case.action.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.delete(case.log)
    await db_session.flush()
    outcome = await reconcile(db_session, case)
    assert outcome.disposition == "unknown" and not outcome.changed
    assert case.action.status == "running" and case.action.row_version == 3


@pytest.mark.parametrize("tool", ["user.create", "user.export"])
async def test_positive_success_is_read_without_fence(db_session, atomic_case, tool):
    case = atomic_case
    case.action.execute_tool_name = case.log.tool_name = tool
    case.action.execution_owner = new_execution_owner(tool)
    await db_session.flush()
    claim = ClaimBinding.from_action(case.action, case.log.log_id)
    locked = await atomic_completion_service.lock_claim(
        db_session, claim, tenant=case.tenant
    )
    await atomic_completion_service.complete_success(
        db_session,
        locked,
        result=ToolResult.success({"value": 1}),
        deps=case.deps,
        duration_ms=5,
        tenant=case.tenant,
    )
    outcome = await reconcile(db_session, case)
    assert outcome.disposition == "succeeded" and not outcome.changed
    assert outcome.action.result_data == {"value": 1}
    claimed = await reconcile(db_session, case, expected_claim=claim, allow_fence=True)
    assert claimed.disposition == "succeeded" and not claimed.changed


@pytest.mark.parametrize("corruption", [None, "error", "owner", "file", "log"])
async def test_failed_requires_positive_db_terminal_proof(
    db_session, atomic_case, corruption
):
    case = atomic_case
    locked = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant
    )
    await atomic_completion_service.complete_failed(
        db_session,
        locked,
        error_code="TEST_CONFIRMED_ROLLBACK",
        duration_ms=4,
        tenant=case.tenant,
    )
    if corruption == "error":
        case.action.error_code = case.log.error_code = None
    elif corruption == "owner":
        case.action.execution_owner = "legacy-owner"
    elif corruption == "file":
        case.action.execute_tool_name = case.log.tool_name = "user.export"
        case.action.execution_owner = new_execution_owner("user.export")
    elif corruption == "log":
        case.log.trace_id = "bad-trace"
    await db_session.flush()
    before = facts(case)
    outcome = await reconcile(db_session, case)
    assert outcome.disposition == ("failed" if corruption is None else "unknown")
    assert not outcome.changed and facts(case) == before


async def test_scope_and_confirmation_mismatch_never_return_foreign_action(
    db_session, atomic_case
):
    from app.modules.ai.service.completion_recovery_service import (  # noqa: PLC0415
        completion_recovery_service,
    )

    case = atomic_case
    for confirmation_id, tenant in (
        ("missing-confirmation", case.tenant),
        (case.action.confirmation_id, tenant_context(tenant_id=987, actor_user_id=1)),
        (case.action.confirmation_id, tenant_context(tenant_id=0, actor_user_id=1)),
    ):
        outcome = await completion_recovery_service.reconcile(
            db_session,
            confirmation_id,
            tenant=tenant,
            expected_claim=case.claim,
            allow_fence=True,
        )
        assert outcome.disposition == "unknown" and not outcome.changed
        assert outcome.action is None


async def test_failure_after_action_cas_propagates_for_caller_rollback(
    db_session, atomic_case, monkeypatch
):
    case = atomic_case
    before = facts(case)
    monkeypatch.setattr(
        atomic_completion_service,
        "_finish_log",
        AsyncMock(
            side_effect=BusinessRuleException(
                "Required log failed", error_code="AI_PREPARED_ACTION_BINDING_INVALID"
            )
        ),
    )
    with pytest.raises(BusinessRuleException):
        async with db_session.begin_nested():
            await reconcile(
                db_session, case, expected_claim=case.claim, allow_fence=True
            )
    await db_session.refresh(case.action)
    await db_session.refresh(case.log)
    assert facts(case) == before


@pytest.mark.parametrize("missing", ["conversation", "source"])
async def test_prelocked_validator_requires_complete_context(
    db_session, atomic_case, missing
):
    case = atomic_case
    locked = await atomic_completion_service.lock_claim(
        db_session, case.claim, tenant=case.tenant
    )
    await atomic_completion_service.complete_success(
        db_session,
        locked,
        result=ToolResult.success({"value": 1}),
        deps=case.deps,
        duration_ms=1,
        tenant=case.tenant,
    )
    valid = atomic_completion_service.validate_locked_terminal(
        case.action,
        case.log,
        tenant=case.tenant,
        conversation=case.conversation,
        source=case.source,
    )
    assert valid.action is case.action and valid.log is case.log
    with pytest.raises(BusinessRuleException):
        atomic_completion_service.validate_locked_terminal(
            case.action,
            case.log,
            tenant=case.tenant,
            conversation=None if missing == "conversation" else case.conversation,
            source=None if missing == "source" else case.source,
        )


async def test_forced_fence_still_requires_lease_and_clean_running_error(
    db_session, atomic_case
):
    case = atomic_case
    for lease, error in ((None, None), (datetime.now(UTC), "UNVERIFIED_ERROR")):
        case.action.execution_lease_expires_at = lease
        case.action.error_code = case.log.error_code = error
        await db_session.flush()
        before = facts(case)
        outcome = await reconcile(
            db_session, case, expected_claim=case.claim, allow_fence=True
        )
        assert outcome.disposition == "unknown" and not outcome.changed
        assert facts(case) == before
