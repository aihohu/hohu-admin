"""Prepared-action freezing and snapshot verification tests."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from sqlalchemy import select

from app.core.exceptions import BusinessRuleException
from app.core.file_storage import MockFileStorage, reset_file_storage_for_test
from app.modules.ai.agents.hitl.constants import (
    AiOperationStatus,
    PreparedActionStatus,
)
from app.modules.ai.agents.hitl.manager import PendingPayload
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.prepared_action_service import (
    canonical_payload_hash,
    prepared_action_service,
)
from app.modules.ai.service.result_projection_service import (
    result_projection_service,
)
from app.modules.system.constants import ImportBatchStatus
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import UserImportBatch


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
        "prepare_tool_name": "user.import_preview",
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
        "resolved_model_id": 501,
        "resolved_provider_id": 601,
        "projection_dependency_message_ids": [71, 72],
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }


def test_canonical_hash_is_key_order_independent_and_type_aware() -> None:
    assert canonical_payload_hash({"b": 2, "a": 1}) == canonical_payload_hash(
        {"a": 1, "b": 2}
    )
    assert canonical_payload_hash({"value": 1}) != canonical_payload_hash(
        {"value": "1"}
    )


def test_scope_bound_action_rejects_data_scope_drift() -> None:
    """A scope-bound action cannot execute under a different resolved scope."""
    action = type("Action", (), {"data_scope_hash": "frozen-scope"})()

    with pytest.raises(BusinessRuleException) as exc_info:
        prepared_action_service.validate_data_scope_snapshot(
            action,
            current_data_scope_hash="current-scope",
        )

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"


def test_finite_action_does_not_require_a_scope_hash() -> None:
    """Finite-target actions continue through their domain subject checks."""
    action = type("Action", (), {"data_scope_hash": None})()

    prepared_action_service.validate_data_scope_snapshot(
        action,
        current_data_scope_hash="current-scope",
    )


@pytest.mark.parametrize(
    ("tool_name", "frozen_args", "snapshot", "expected_refs"),
    [
        (
            "dept.create",
            {"parent_id": 20, "dept_name": "New department"},
            {
                "business": {
                    "version": "phase3-dept-write/v1",
                    "facts": {
                        "deptIds": [10, 20],
                        "impact": {"300": {}},
                        "affectedRoles": [{"roleId": "400"}],
                        "leader": {"userId": "500"},
                    },
                }
            },
            {
                ("dept", "10"),
                ("dept", "20"),
                ("managed_role", "400"),
                ("user", "300"),
                ("user", "500"),
            },
        ),
        (
            "dept.update",
            {"dept_id": 20, "status": "2"},
            {
                "business": {
                    "version": "phase3-dept-write/v1",
                    "facts": {
                        "deptIds": [10, 20],
                        "impact": {"300": {}},
                        "affectedRoles": [{"roleId": "400"}],
                        "leader": None,
                    },
                }
            },
            {
                ("dept", "10"),
                ("dept", "20"),
                ("managed_role", "400"),
                ("user", "300"),
            },
        ),
        (
            "dept.move",
            {"dept_id": 20, "new_parent_id": 30},
            {
                "business": {
                    "version": "phase3-dept-write/v1",
                    "facts": {
                        "deptIds": [10, 20, 30],
                        "impact": {},
                        "affectedRoles": [],
                        "leader": None,
                    },
                }
            },
            {("dept", "10"), ("dept", "20"), ("dept", "30")},
        ),
        (
            "role.create",
            {"role_code": "R_NEW", "dept_ids": [10, 20]},
            {"business": {"version": "phase3-role-write/v1"}},
            {("dept", "10"), ("dept", "20")},
        ),
        (
            "role.update",
            {"role_id": 400, "status": "2"},
            {"business": {"version": "phase3-role-write/v1"}},
            {("managed_role", "400")},
        ),
        (
            "role.update_menus",
            {"role_id": 400, "menu_ids": [1, 2]},
            {"business": {"version": "phase3-role-write/v1"}},
            {("managed_role", "400")},
        ),
        (
            "role.update_agents",
            {"role_id": 400, "agent_ids": [1, 2]},
            {
                "business": {
                    "targetRole": {"roleId": "400"},
                    "members": [],
                }
            },
            {("managed_role", "400")},
        ),
    ],
)
def test_phase3_management_actions_freeze_complete_projection_targets(
    tool_name: str,
    frozen_args: dict,
    snapshot: dict,
    expected_refs: set[tuple[str, str]],
) -> None:
    refs = prepared_action_service._build_subject_refs(
        execute_tool_name=tool_name,
        frozen_args=frozen_args,
        snapshot=snapshot,
        subject_ref=None,
        projection_kind=None,
    )

    assert {(item["type"], item["id"]) for item in refs} == expected_refs


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
    assert action.resolved_model_id == 501
    assert action.resolved_provider_id == 601
    assert action.tool_codes == ["user.import_execute", "user.import_preview"]
    assert action.subject_refs == [
        {"type": "user_import_batch", "id": "batch-prepared-1"}
    ]
    assert action.projection_dependency_message_ids == ["71", "72"]
    assert len(action.subject_refs_hash) == 64


@pytest.mark.parametrize(
    "status",
    [
        PreparedActionStatus.PREPARED,
        PreparedActionStatus.PENDING_CONFIRMATION,
        PreparedActionStatus.APPROVED,
    ],
)
async def test_conversation_delete_expires_non_running_actions_and_logs(
    db_session,
    status: PreparedActionStatus,
) -> None:
    kwargs = _create_kwargs()
    kwargs["confirmation_id"] = f"cid_delete_{status.value}"
    kwargs["execute_tool_call_id"] = f"tc_delete_{status.value}"
    action = await prepared_action_service.create_pending(db_session, **kwargs)
    action.status = status.value
    action.execution_owner = "worker-1"
    action.execution_lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    log_id = await operation_log_service.start_operation(
        db_session,
        trace_id=action.trace_id,
        conversation_id=action.conversation_id,
        tenant_id=action.tenant_id,
        source_user_message_id=action.source_user_message_id,
        agent_code=action.agent_code,
        user_id=action.user_id,
        tool_name=action.execute_tool_name,
        tool_call_id=action.execute_tool_call_id,
        args_hash=action.args_hash,
        args_summary="safe metadata",
        risk_level="high",
        execution_mode="hitl",
        status=AiOperationStatus.PENDING_CONFIRMATION,
        confirmation_id=action.confirmation_id,
    )

    expired = await prepared_action_service.expire_for_conversation_delete(
        db_session,
        conversation_id=action.conversation_id,
        user_id=action.user_id,
        tenant_id=action.tenant_id,
    )
    await db_session.flush()
    log = await db_session.get(AiOperationLog, log_id)

    assert [item.action_id for item in expired] == [action.action_id]
    assert action.status == PreparedActionStatus.EXPIRED.value
    assert action.error_code == "AI_CONVERSATION_DELETED"
    assert action.finished_at is not None
    assert action.execution_owner is None
    assert action.execution_lease_expires_at is None
    assert action.guard_owner_token is None
    assert log is not None
    assert log.status == AiOperationStatus.EXPIRED.value
    assert log.error_code == "AI_CONVERSATION_DELETED"
    assert log.finished_at is not None


async def test_conversation_delete_rejects_running_action_without_partial_expiry(
    db_session,
) -> None:
    first_kwargs = _create_kwargs()
    first_kwargs["confirmation_id"] = "cid_delete_pending"
    first_kwargs["execute_tool_call_id"] = "tc_delete_pending"
    pending = await prepared_action_service.create_pending(db_session, **first_kwargs)
    running_kwargs = _create_kwargs()
    running_kwargs["confirmation_id"] = "cid_delete_running"
    running_kwargs["execute_tool_call_id"] = "tc_delete_running"
    running = await prepared_action_service.create_pending(db_session, **running_kwargs)
    running.status = PreparedActionStatus.RUNNING.value
    running.execution_owner = "worker-running"
    running.execution_lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    await db_session.flush()

    with pytest.raises(BusinessRuleException) as exc_info:
        await prepared_action_service.expire_for_conversation_delete(
            db_session,
            conversation_id=pending.conversation_id,
            user_id=pending.user_id,
            tenant_id=pending.tenant_id,
        )

    assert exc_info.value.error_code == "AI_ACTION_RUNNING"
    assert exc_info.value.code == 409
    assert pending.status == PreparedActionStatus.PENDING_CONFIRMATION.value
    assert running.status == PreparedActionStatus.RUNNING.value


async def test_conversation_delete_recovers_expired_running_lease(
    db_session,
) -> None:
    kwargs = _create_kwargs()
    kwargs["confirmation_id"] = "cid_delete_stale_running"
    kwargs["execute_tool_call_id"] = "tc_delete_stale_running"
    action = await prepared_action_service.create_pending(db_session, **kwargs)
    action.status = PreparedActionStatus.RUNNING.value
    action.execution_owner = "stale-worker"
    action.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    log_id = await operation_log_service.start_operation(
        db_session,
        trace_id=action.trace_id,
        conversation_id=action.conversation_id,
        tenant_id=action.tenant_id,
        source_user_message_id=action.source_user_message_id,
        agent_code=action.agent_code,
        user_id=action.user_id,
        tool_name=action.execute_tool_name,
        tool_call_id=action.execute_tool_call_id,
        args_hash=action.args_hash,
        args_summary="safe metadata",
        risk_level="high",
        execution_mode="hitl",
        status=AiOperationStatus.RUNNING,
        confirmation_id=action.confirmation_id,
    )

    recovered = await prepared_action_service.expire_for_conversation_delete(
        db_session,
        conversation_id=action.conversation_id,
        user_id=action.user_id,
        tenant_id=action.tenant_id,
    )
    await db_session.flush()
    log = await db_session.get(AiOperationLog, log_id)

    assert [item.action_id for item in recovered] == [action.action_id]
    assert action.status == PreparedActionStatus.FAILED.value
    assert action.error_code == "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
    assert action.finished_at is not None
    assert action.execution_owner is None
    assert action.execution_lease_expires_at is None
    assert action.guard_owner_token is None
    assert log is not None
    assert log.status == AiOperationStatus.FAILED.value
    assert log.error_code == "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
    assert log.finished_at is not None


async def test_create_pending_rejects_missing_frozen_model(db_session) -> None:
    kwargs = _create_kwargs()
    kwargs["resolved_model_id"] = None

    with pytest.raises(BusinessRuleException) as exc_info:
        await prepared_action_service.create_pending(db_session, **kwargs)

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_BINDING_INVALID"


async def test_create_pending_supports_direct_hitl_with_same_state_machine(
    db_session,
) -> None:
    kwargs = _create_kwargs()
    snapshot = {"tool": "user.batch_delete", "argsHash": "hash", "dryRun": None}
    kwargs.update(
        confirmation_id="cid_direct_action_001",
        prepare_tool_call_id=None,
        prepare_tool_name=None,
        execute_tool_call_id="tc_direct_001",
        execute_tool_name="user.batch_delete",
        frozen_args={"user_ids": [9002]},
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


async def test_user_create_without_department_has_complete_empty_input_targets(
    db_session,
) -> None:
    kwargs = _create_kwargs()
    snapshot = {"tool": "user.create", "argsHash": "hash", "dryRun": None}
    kwargs.update(
        confirmation_id="cid_direct_create_001",
        prepare_tool_call_id=None,
        prepare_tool_name=None,
        execute_tool_call_id="tc_direct_create_001",
        execute_tool_name="user.create",
        frozen_args={"user_name": "new-user", "primary_dept_id": None},
        snapshot=snapshot,
        snapshot_hash=canonical_payload_hash(snapshot),
        subject_ref=None,
        interaction_flow="direct",
        requested_outcome="direct",
    )

    action = await prepared_action_service.create_pending(db_session, **kwargs)

    assert action.subject_refs == []
    assert len(action.subject_refs_hash) == 64


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


async def test_batch_delete_snapshot_rejects_identity_drift(db_session) -> None:
    target = User(
        user_id=91001,
        user_name="approved-target",
        nickname="Approved target",
        hashed_password="$2b$12$dummy",
        user_phone="13900000001",
        status="1",
    )
    db_session.add(target)
    await db_session.flush()

    kwargs = _create_kwargs()
    snapshot = {
        "tool": "user.batch_delete",
        "argsHash": canonical_payload_hash(
            {"user_ids": [91001], "user_names": None, "phones": None}
        ),
        "business": {
            "targets": [
                {
                    "userId": "91001",
                    "userName": "approved-target",
                    "userPhone": "13900000001",
                }
            ]
        },
    }
    kwargs.update(
        confirmation_id="cid_batch_delete_snapshot_001",
        prepare_tool_call_id=None,
        prepare_tool_name=None,
        execute_tool_call_id="tc_batch_delete_snapshot_001",
        execute_tool_name="user.batch_delete",
        frozen_args={"user_ids": [91001], "user_names": None, "phones": None},
        snapshot=snapshot,
        snapshot_hash=canonical_payload_hash(snapshot),
        subject_ref=None,
        presentation={
            "title": "user.batch_delete",
            "fields": [{"label": "affectedCount", "value": 1}],
            "warnings": ["此操作不可逆，请确认影响范围。"],
        },
        interaction_flow="direct",
        requested_outcome="direct",
    )
    action = await prepared_action_service.create_pending(db_session, **kwargs)

    await prepared_action_service.validate_snapshot(db_session, action)
    target.user_phone = "13900000999"
    await db_session.flush()

    with pytest.raises(BusinessRuleException) as exc_info:
        await prepared_action_service.validate_snapshot(db_session, action)

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"


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

    lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    running = await prepared_action_service.transition_status(
        db_session,
        action_id=action.action_id,
        expected_status=PreparedActionStatus.APPROVED,
        expected_version=2,
        target_status=PreparedActionStatus.RUNNING,
        execution_owner="test-executor",
        execution_lease_expires_at=lease_expires_at,
    )
    assert running is not None
    renewed = await prepared_action_service.renew_execution_lease(
        db_session,
        action_id=action.action_id,
        execution_owner="test-executor",
        lease_expires_at=lease_expires_at + timedelta(minutes=1),
    )
    wrong_owner_renewed = await prepared_action_service.renew_execution_lease(
        db_session,
        action_id=action.action_id,
        execution_owner="other-executor",
        lease_expires_at=lease_expires_at + timedelta(minutes=2),
    )
    result_lineage = result_projection_service.freeze_lineage(
        tenant_id=0,
        agent_code="user_mgmt",
        tool_codes=["user.reset_password"],
        subject_refs=[{"type": "user", "id": "99002"}],
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
        result_lineage=result_lineage,
        replace_result_lineage=True,
    )

    assert renewed is True
    assert wrong_owner_renewed is False
    assert succeeded is not None
    assert succeeded.row_version == 4
    assert succeeded.result_data == {"successCount": 2}
    assert succeeded.duration_ms == 12
    assert succeeded.finished_at is not None
    assert succeeded.execution_owner is None
    assert succeeded.execution_lease_expires_at is None
    assert succeeded.subject_refs == [{"type": "user", "id": "99002"}]
    assert succeeded.subject_refs_hash == result_lineage.subject_refs_hash


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
