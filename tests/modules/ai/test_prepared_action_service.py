"""Task 35a.2 prepared-action freezing and snapshot verification."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.service.prepared_action_service import (
    canonical_payload_hash,
    prepared_action_service,
)
from app.modules.system.user.constants import ImportBatchStatus
from app.modules.system.user.models import UserImportBatch


def _snapshot(*, records_hash: str = "records-1") -> dict:
    return {
        "batch_id": "batch-prepared-1",
        "file_sha256": "file-1",
        "records_hash": records_hash,
        "operator_id": 9001,
        "total": 2,
        "summary": {
            "new": 2,
            "exists": 0,
            "conflict": 0,
            "outOfScope": 0,
        },
    }


def _create_kwargs() -> dict:
    snapshot = _snapshot()
    return {
        "confirmation_id": "cid_prepared_action_001",
        "prepare_tool_call_id": "tc_prepare_001",
        "execute_tool_call_id": "tc_execute_001",
        "execute_tool_name": "user.import_execute",
        "frozen_args": {
            "preview_token": "preview-secret",
            "reason": "quarterly import",
            "on_conflict": "skip",
            "sync_mode": "CREATE_ONLY",
        },
        "snapshot": snapshot,
        "snapshot_hash": canonical_payload_hash(snapshot),
        "subject_ref": {"type": "user_import_batch", "id": "batch-prepared-1"},
        "presentation": {
            "title": "Import 2 users",
            "fields": {"new": 2, "onConflict": "skip"},
            "warnings": [],
        },
        "user_id": 9001,
        "tenant_id": 77,
        "conversation_id": 100,
        "source_user_message_id": 101,
        "trace_id": "tr_test_prepared_001",
        "agent_code": "user_mgmt",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }


def test_canonical_hash_is_key_order_independent_and_type_aware() -> None:
    assert canonical_payload_hash({"b": 2, "a": 1}) == canonical_payload_hash(
        {"a": 1, "b": 2}
    )
    assert canonical_payload_hash({"value": 1}) != canonical_payload_hash(
        {"value": "1"}
    )


async def test_create_pending_freezes_policy_and_trusted_identity(db_session) -> None:
    action = await prepared_action_service.create_pending(
        db_session, **_create_kwargs()
    )

    assert action.status == "pending_confirmation"
    assert action.row_version == 1
    assert action.interaction_flow == "prepared"
    assert action.requested_outcome == "execute_if_approved"
    assert action.approval_mode == "hitl"
    assert action.dispatch_mode == "inline"
    assert action.args_hash == canonical_payload_hash(action.frozen_args)
    assert action.snapshot_hash == canonical_payload_hash(action.snapshot)
    assert action.user_id == 9001
    assert action.tenant_id == 77
    assert action.source_user_message_id == 101


async def test_pending_binding_rejects_changed_execute_args(db_session) -> None:
    action = await prepared_action_service.create_pending(
        db_session, **_create_kwargs()
    )
    pending = PendingPayload(
        user_id=9001,
        tenant_id=77,
        conversation_id=100,
        tool_call_id="tc_execute_001",
        trace_id="tr_test_prepared_001",
        tool_name="user.import_execute",
        args={
            **action.frozen_args,
            "on_conflict": "overwrite",
        },
        dry_run_result=None,
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        source_user_message_id=101,
        agent_code="user_mgmt",
    )

    with pytest.raises(BusinessRuleException) as exc_info:
        prepared_action_service.validate_pending_binding(action, pending)

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_BINDING_INVALID"


async def test_user_import_snapshot_stale_is_rejected_before_execute(
    db_session,
) -> None:
    batch = UserImportBatch(
        batch_id="batch-prepared-1",
        operator_id=9001,
        filename="users.xlsx",
        file_sha256="file-1",
        records_hash="records-1",
        total_rows=2,
        preview_token="preview-secret",
        summary_new=2,
        summary_exists=0,
        summary_conflict=0,
        summary_out_of_scope=0,
        on_conflict="skip",
        reason="quarterly import",
        status=ImportBatchStatus.PREVIEW_DONE,
    )
    db_session.add(batch)
    await db_session.flush()
    action = await prepared_action_service.create_pending(
        db_session, **_create_kwargs()
    )

    await prepared_action_service.validate_snapshot(db_session, action)
    batch.records_hash = "records-changed-after-preview"
    await db_session.flush()

    with pytest.raises(BusinessRuleException) as exc_info:
        await prepared_action_service.validate_snapshot(db_session, action)

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
