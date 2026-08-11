"""Task 35a.2 prepared-action freezing and snapshot verification."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from sqlalchemy import select

from app.core.exceptions import BusinessRuleException
from app.core.file_storage import MockFileStorage, reset_file_storage_for_test
from app.modules.ai.agents.hitl.constants import PreparedActionStatus
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.service.prepared_action_service import (
    canonical_payload_hash,
    prepared_action_service,
)
from app.modules.system.models.user import User
from app.modules.system.user.constants import ImportBatchStatus
from app.modules.system.user.models import UserImportBatch


def _snapshot(*, records_hash: str = "records-1", file_sha256: str = "file-1") -> dict:
    return {
        "batch_id": "batch-prepared-1",
        "file_sha256": file_sha256,
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
            "fields": [
                {"label": "new", "value": 2},
                {"label": "onConflict", "value": "skip"},
            ],
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


async def test_create_pending_supports_direct_hitl_with_same_state_machine(
    db_session,
) -> None:
    kwargs = _create_kwargs()
    snapshot = {"tool": "user.batch_delete", "argsHash": "hash", "dryRun": None}
    kwargs.update(
        confirmation_id="cid_direct_action_001",
        prepare_tool_call_id=None,
        execute_tool_call_id="tc_direct_001",
        execute_tool_name="user.batch_delete",
        frozen_args={},
        snapshot=snapshot,
        snapshot_hash=canonical_payload_hash(snapshot),
        subject_ref=None,
        presentation={
            "title": "user.batch_delete",
            "fields": [],
            "warnings": ["此操作不可逆，请确认影响范围。"],
        },
        interaction_flow="direct",
        requested_outcome="direct",
    )

    action = await prepared_action_service.create_pending(db_session, **kwargs)

    assert action.interaction_flow == "direct"
    assert action.requested_outcome == "direct"
    assert action.prepare_tool_call_id is None
    assert action.status == "pending_confirmation"


@pytest.mark.parametrize(
    "presentation",
    [
        {"title": "Import", "fields": {"new": 2}, "warnings": []},
        {
            "title": "Import",
            "fields": [{"label": "previewToken", "value": "secret"}],
            "warnings": [],
        },
        {
            "title": "Import",
            "fields": [{"label": "new", "value": {"count": 2}}],
            "warnings": [],
        },
        {
            "title": "Import",
            "summary": "preview_token=server-only-token",
            "fields": [],
            "warnings": [],
        },
        {
            "title": "Import",
            "fields": [],
            "warnings": ["See https://private.example/import/1"],
        },
    ],
)
async def test_confirmation_presentation_rejects_noncanonical_or_sensitive_fields(
    db_session, presentation
) -> None:
    kwargs = _create_kwargs()
    kwargs["presentation"] = presentation

    with pytest.raises(BusinessRuleException) as exc_info:
        await prepared_action_service.create_pending(db_session, **kwargs)

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_BINDING_INVALID"


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
    file_bytes = b"prepared import artifact"
    file_sha256 = sha256(file_bytes).hexdigest()
    storage = MockFileStorage()
    storage_key = await storage.save(
        file_bytes,
        mime_type="text/csv",
        namespace="import-preview",
        suffix=".csv",
    )
    reset_file_storage_for_test(storage)
    try:
        batch = UserImportBatch(
            batch_id="batch-prepared-1",
            operator_id=9001,
            filename="users.xlsx",
            file_sha256=file_sha256,
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
            file_storage_key=storage_key,
        )
        db_session.add(batch)
        await db_session.flush()
        kwargs = _create_kwargs()
        snapshot = _snapshot(file_sha256=file_sha256)
        kwargs.update(
            snapshot=snapshot,
            snapshot_hash=canonical_payload_hash(snapshot),
        )
        action = await prepared_action_service.create_pending(db_session, **kwargs)

        await prepared_action_service.validate_snapshot(db_session, action)
        batch.records_hash = "records-changed-after-preview"
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc_info:
            await prepared_action_service.validate_snapshot(db_session, action)

        assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
    finally:
        reset_file_storage_for_test(None)


async def test_action_status_cas_allows_only_one_execution_claim(db_session) -> None:
    action = await prepared_action_service.create_pending(
        db_session, **_create_kwargs()
    )

    approved = await prepared_action_service.transition_status(
        db_session,
        action_id=action.action_id,
        expected_status=PreparedActionStatus.PENDING_CONFIRMATION,
        expected_version=1,
        target_status=PreparedActionStatus.APPROVED,
        approved_by=9001,
    )
    duplicate = await prepared_action_service.transition_status(
        db_session,
        action_id=action.action_id,
        expected_status=PreparedActionStatus.PENDING_CONFIRMATION,
        expected_version=1,
        target_status=PreparedActionStatus.APPROVED,
        approved_by=9001,
    )

    assert approved is not None
    assert approved.status == PreparedActionStatus.APPROVED
    assert approved.row_version == 2
    assert approved.approved_by == 9001
    assert approved.approved_at is not None
    assert duplicate is None

    running = await prepared_action_service.transition_status(
        db_session,
        action_id=action.action_id,
        expected_status=PreparedActionStatus.APPROVED,
        expected_version=2,
        target_status=PreparedActionStatus.RUNNING,
    )
    succeeded = await prepared_action_service.transition_status(
        db_session,
        action_id=action.action_id,
        expected_status=PreparedActionStatus.RUNNING,
        expected_version=3,
        target_status=PreparedActionStatus.SUCCEEDED,
        result_data={"successCount": 2},
        result_ui={"viewType": "rows_affected", "viewData": {"count": 2}},
        duration_ms=12,
    )

    assert running is not None
    assert succeeded is not None
    assert succeeded.row_version == 4
    assert succeeded.result_data == {"successCount": 2}
    assert succeeded.duration_ms == 12
    assert succeeded.finished_at is not None


async def test_pending_query_is_scoped_and_requires_active_source(db_session) -> None:
    owner = (
        await db_session.execute(select(User).where(User.user_name == "admin"))
    ).scalar_one()
    conversation = AiConversation(user_id=owner.user_id, title="prepared pending")
    db_session.add(conversation)
    await db_session.flush()
    source = AiMessage(
        conversation_id=conversation.conversation_id,
        role="user",
        content="import users",
        message_type="text",
        trace_id="tr_test_pending_query",
        is_active=True,
    )
    db_session.add(source)
    await db_session.flush()
    kwargs = _create_kwargs()
    kwargs.update(
        user_id=owner.user_id,
        tenant_id=0,
        conversation_id=conversation.conversation_id,
        source_user_message_id=source.message_id,
        trace_id="tr_test_pending_query",
    )
    action = await prepared_action_service.create_pending(db_session, **kwargs)

    owned = await prepared_action_service.list_pending_for_conversation(
        db_session,
        conversation_id=conversation.conversation_id,
        user_id=owner.user_id,
        tenant_id=0,
    )
    other_tenant = await prepared_action_service.list_pending_for_conversation(
        db_session,
        conversation_id=conversation.conversation_id,
        user_id=owner.user_id,
        tenant_id=999,
    )

    assert [item.action_id for item in owned] == [action.action_id]
    assert other_tenant == []
    projection = prepared_action_service.to_pending_out(owned[0]).model_dump(
        by_alias=True, mode="json"
    )
    assert projection["actionId"] == str(action.action_id)
    assert projection["sourceUserMessageId"] == str(source.message_id)
    assert projection["presentation"] == action.presentation
    assert "frozenArgs" not in projection
    assert "snapshot" not in projection
    assert "previewToken" not in projection

    source.is_active = False
    await db_session.flush()
    inactive = await prepared_action_service.list_pending_for_conversation(
        db_session,
        conversation_id=conversation.conversation_id,
        user_id=owner.user_id,
        tenant_id=0,
    )
    assert inactive == []
