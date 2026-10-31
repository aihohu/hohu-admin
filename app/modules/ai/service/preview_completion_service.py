"""Positive preview completion facts. No recovery execution or transaction owner."""

import json
import re
from datetime import UTC, timedelta
from hashlib import sha256

from sqlalchemy import select, text

from app.core.authorization_plan import ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.gateway.result import PreparedActionProposal
from app.modules.ai.agents.hitl.events import _ui_to_dict
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.operation_log_service import (
    _now_not_before,
    build_target_summary,
    operation_log_service,
)
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.reconciliation_annotation import require_unannotated
from app.modules.system.constants import ImportBatchStatus
from app.modules.system.service.user_transfer_artifact_service import (
    batch_snapshot,
    read_batch,
    verify_file,
)


def invalid_preview():
    return BusinessRuleException(
        "预览完成或确认交接尚待核对", error_code="AI_EXECUTION_OUTCOME_UNKNOWN"
    )


def validate_context(log, conversation, source, *, tenant_id, user_id):
    if log is not None:
        require_unannotated(log.result_summary)
    if (
        log is None
        or conversation is None
        or source is None
        or log.tenant_id != tenant_id
        or log.user_id != user_id
        or log.tool_name != "user.import_preview"
        or log.readonly_snapshot
        or log.confirmation_id is not None
        or log.agent_code != "user_mgmt"
        or not re.fullmatch(r"[0-9a-f]{64}", log.args_hash or "")
        or not log.tool_call_id
        or not log.trace_id
        or conversation.tenant_id != tenant_id
        or conversation.user_id != user_id
        or conversation.conversation_id != log.conversation_id
        or source.tenant_id != tenant_id
        or source.role != "user"
        or source.conversation_id != log.conversation_id
        or source.message_id != log.source_user_message_id
        or source.trace_id != log.trace_id
    ):
        raise invalid_preview()


async def lock_preview(db, *, log_id, tool_call_id, args_hash, deps):
    await db.execute(text("SELECT set_config('lock_timeout', '2s', true)"))
    if not await prepared_action_service.lock_source_binding(
        db,
        conversation_id=deps.conversation_id or 0,
        source_user_message_id=deps.source_user_message_id or 0,
        user_id=deps.user.user_id,
        tenant=deps.tenant,
    ):
        raise invalid_preview()
    log = await operation_log_service._get(db, log_id, tenant=deps.tenant)
    conversation = await db.get(AiConversation, deps.conversation_id)
    source = await db.get(AiMessage, deps.source_user_message_id)
    validate_context(
        log, conversation, source, tenant_id=deps.tenant_id, user_id=deps.user.user_id
    )
    if (
        log.tool_call_id != tool_call_id
        or log.args_hash != args_hash
        or log.trace_id != deps.trace_id
        or log.conversation_id != deps.conversation_id
        or log.source_user_message_id != deps.source_user_message_id
        or await db.scalar(
            select(AiPreparedAction.action_id).where(
                AiPreparedAction.tenant_id == deps.tenant_id,
                AiPreparedAction.execute_tool_call_id == tool_call_id,
            )
        )
        is not None
    ):
        raise invalid_preview()
    return log


async def validate_proposal(
    db,
    *,
    proposal,
    tenant_id,
    user_id,
    execution=None,
    verify_artifact=True,
    _planned=None,
):
    if (
        not isinstance(proposal, PreparedActionProposal)
        or build_target_summary([proposal.subject_ref]) is None
        or proposal.subject_ref.get("type") != "user_import_batch"
    ):
        raise invalid_preview()
    if _planned is not None:
        _planned.assert_session(db)
        _planned.assert_held(
            ResourceKey(K.IMPORT_BATCH, tenant_id, proposal.subject_ref["id"])
        )
    batch = await read_batch(
        db, proposal.subject_ref["id"], tenant_id, lock=_planned is None
    )
    frozen = proposal.frozen_args
    if (
        batch is None
        or batch.operator_id != user_id
        or batch.status != ImportBatchStatus.PREVIEW_DONE
        or set(frozen) != {"preview_token", "reason", "on_conflict", "sync_mode"}
        or frozen["preview_token"] != batch.preview_token
        or frozen["reason"] != batch.reason
        or frozen["on_conflict"] != batch.on_conflict
        or frozen["sync_mode"] not in {"CREATE_ONLY", "UPDATE_PROFILE", "FULL_SYNC"}
        or proposal.snapshot != batch_snapshot(batch)
    ):
        raise invalid_preview()
    expires = proposal.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires != batch.created_at + timedelta(minutes=10):
        raise invalid_preview()
    prepared_action_service.validate_presentation(proposal.presentation)
    if not verify_artifact:
        return batch
    data = await verify_file(
        batch.file_storage_key,
        tenant_id=tenant_id,
        resource_id=batch.batch_id,
        role="preview_source",
        execution=execution,
    )
    if sha256(data).hexdigest() != batch.file_sha256:
        raise invalid_preview()
    return batch


async def complete_preview(db, *, log, result, args, execution, duration_ms, deps):
    if log.status != "running" or not result.ok:
        raise invalid_preview()
    proposal = result.prepared_action
    batch = await validate_proposal(
        db,
        proposal=proposal,
        tenant_id=log.tenant_id,
        user_id=log.user_id,
        execution=execution,
    )
    if (
        result.projection is None
        or tuple(result.projection.subject_refs) != (proposal.subject_ref,)
        or not isinstance(result.data, dict)
        or result.data.get("batchId") != batch.batch_id
        or proposal.frozen_args["reason"] != args.get("reason")
        or proposal.frozen_args["on_conflict"] != args.get("on_conflict", "skip")
        or proposal.frozen_args["sync_mode"] != args.get("sync_mode", "CREATE_ONLY")
    ):
        raise invalid_preview()
    # Existing audit summary carries business facts, never raw tokens or a
    # protocol generation. Retention of the batch does not erase this history.
    log.result_summary = json.dumps(
        {
            "status": "success",
            "duration_ms": duration_ms,
            "audit": {
                **batch_snapshot(batch),
                "on_conflict": batch.on_conflict,
                "preview_data": result.data,
                "preview_ui": _ui_to_dict(result.ui),
                "data_scope_hash": deps.data_scope_hash,
                "projection_dependency_message_ids": list(
                    deps.projection_dependency_message_ids
                ),
                "proposal_hash": compute_args_hash(
                    {
                        "args": proposal.frozen_args,
                        "snapshot": proposal.snapshot,
                        "presentation": proposal.presentation,
                    }
                ),
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    log.target_summary = build_target_summary([proposal.subject_ref])
    log.status = "success"
    log.error_code = None
    log.finished_at = _now_not_before(log.queued_at, log.started_at)
    log.duration_ms = duration_ms
    await db.flush()


async def verify_historical_preview(
    db, log, conversation, source, *, tenant_id, user_id
):
    validate_context(log, conversation, source, tenant_id=tenant_id, user_id=user_id)
    if (
        log.status != "success"
        or log.error_code is not None
        or log.finished_at is None
        or log.started_at is None
        or log.finished_at < log.started_at
        or log.duration_ms is None
        or log.duration_ms < 0
    ):
        raise invalid_preview()
    try:
        refs = json.loads(log.target_summary)
        value = json.loads(log.result_summary)
        audit = value["audit"]
        if (
            not isinstance(audit, dict)
            or set(audit)
            != {
                "batch_id",
                "file_sha256",
                "records_hash",
                "operator_id",
                "total",
                "summary",
                "on_conflict",
                "data_scope_hash",
                "projection_dependency_message_ids",
                "proposal_hash",
                "preview_data",
                "preview_ui",
            }
            or type(audit["total"]) is not int
            or audit["total"] < 0
            or not isinstance(audit["preview_data"], dict)
            or audit["preview_data"].get("batchId") != audit["batch_id"]
            or audit["preview_data"].get("total") != audit["total"]
            or audit["preview_data"].get("summary") != audit["summary"]
            or not isinstance(audit["preview_ui"], dict)
            or audit["preview_ui"].get("viewType") != "detail_card"
            or not isinstance(audit["summary"], dict)
            or set(audit["summary"]) != {"new", "exists", "conflict", "outOfScope"}
            or any(type(v) is not int or v < 0 for v in audit["summary"].values())
            or (
                audit["data_scope_hash"] is not None
                and not re.fullmatch(r"[0-9a-f]{64}", audit["data_scope_hash"])
            )
            or not isinstance(audit["projection_dependency_message_ids"], list)
            or any(
                type(v) is not int or v <= 0
                for v in audit["projection_dependency_message_ids"]
            )
            or len(refs) != 1
            or refs[0]["type"] != "user_import_batch"
            or build_target_summary(refs) != log.target_summary
            or refs[0]["id"] != audit["batch_id"]
            or audit["operator_id"] != user_id
            or value["status"] != "success"
            or value["duration_ms"] != log.duration_ms
            or not all(
                re.fullmatch(r"[0-9a-f]{64}", audit[key])
                for key in ("file_sha256", "records_hash", "proposal_hash")
            )
            or audit["on_conflict"] not in {"skip", "overwrite", "fail_fast"}
        ):
            raise invalid_preview()
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        raise invalid_preview() from exc
    batch = await read_batch(db, refs[0]["id"], tenant_id)
    if batch is not None:
        if (
            any(audit.get(k) != v for k, v in batch_snapshot(batch).items())
            or audit["on_conflict"] != batch.on_conflict
        ):
            raise invalid_preview()
    return audit


def preview_lineage(log, audit, tenant):
    from app.modules.ai.service.result_projection_service import (  # noqa: PLC0415
        result_projection_service,
    )

    return result_projection_service.freeze_lineage(
        tenant=tenant,
        agent_code=log.agent_code,
        tool_codes=[log.tool_name],
        subject_refs=[{"type": "user_import_batch", "id": audit["batch_id"]}],
        data_scope_hash=audit.get("data_scope_hash"),
        projection_dependency_message_ids=audit.get(
            "projection_dependency_message_ids", ()
        ),
    )


async def lock_handoff(
    db, *, context, deps, log_id, read_existing=False, _planned=None
):
    """C/source serialization covers both the first continuation and readback."""
    from app.modules.ai.service.result_projection_service import (  # noqa: PLC0415
        result_projection_service,
    )

    if _planned is None:
        await db.execute(text("SELECT set_config('lock_timeout', '2s', true)"))
    else:
        _planned.assert_session(db)
    if (
        not context.execute_tool_call_id
        or not context.confirmation_id
        or (
            _planned is None
            and not await prepared_action_service.lock_source_binding(
                db,
                conversation_id=deps.conversation_id or 0,
                source_user_message_id=deps.source_user_message_id or 0,
                user_id=deps.user.user_id,
                tenant=deps.tenant,
            )
        )
    ):
        raise invalid_preview()
    action_query = (
        select(AiPreparedAction)
        .where(
            AiPreparedAction.tenant_id == deps.tenant_id,
            AiPreparedAction.prepare_tool_call_id == context.prepare_tool_call_id,
        )
        .order_by(AiPreparedAction.action_id)
        .execution_options(populate_existing=True)
    )
    actions = list(
        await db.scalars(
            action_query.with_for_update() if _planned is None else action_query
        )
    )
    if (not read_existing and actions) or (read_existing and len(actions) != 1):
        raise invalid_preview()
    log_query = (
        select(AiOperationLog)
        .where(
            AiOperationLog.tenant_id == deps.tenant_id,
            AiOperationLog.tool_call_id.in_(
                [
                    context.prepare_tool_call_id,
                    context.execute_tool_call_id,
                ]
            ),
        )
        .order_by(AiOperationLog.log_id)
        .execution_options(populate_existing=True)
    )
    logs = list(
        await db.scalars(log_query.with_for_update() if _planned is None else log_query)
    )
    if len(logs) != 2:
        raise invalid_preview()
    if _planned is not None:
        _planned.assert_held(
            ResourceKey(K.CONVERSATION, deps.tenant_id, deps.conversation_id),
            ResourceKey(K.SOURCE_MESSAGE, deps.tenant_id, deps.source_user_message_id),
            ResourceKey(
                K.IMPORT_BATCH, deps.tenant_id, context.proposal.snapshot["batch_id"]
            ),
            *(ResourceKey(K.OPERATION_LOG, deps.tenant_id, row.log_id) for row in logs),
            *(
                ResourceKey(K.PREPARED_ACTION, deps.tenant_id, row.action_id)
                for row in actions
            ),
        )
    parent = next(
        (v for v in logs if v.tool_call_id == context.prepare_tool_call_id), None
    )
    child = next(
        (v for v in logs if v.tool_call_id == context.execute_tool_call_id), None
    )
    audit = await verify_historical_preview(
        db,
        parent,
        await db.get(AiConversation, deps.conversation_id),
        await db.get(AiMessage, deps.source_user_message_id),
        tenant_id=deps.tenant_id,
        user_id=deps.user.user_id,
    )
    proposal = context.proposal
    if (
        child is None
        or child.log_id != log_id
        or child.user_id != deps.user.user_id
        or child.conversation_id != deps.conversation_id
        or child.source_user_message_id != deps.source_user_message_id
        or child.trace_id != deps.trace_id
        or child.agent_code != "user_mgmt"
        or child.tool_name != "user.import_execute"
        or child.readonly_snapshot
        or child.status != "pending_confirmation"
        or child.args_hash != compute_args_hash(proposal.frozen_args)
        or child.confirmation_id
        != (context.confirmation_id if read_existing or _planned is not None else None)
        or audit["proposal_hash"]
        != compute_args_hash(
            {
                "args": proposal.frozen_args,
                "snapshot": proposal.snapshot,
                "presentation": proposal.presentation,
            }
        )
    ):
        raise invalid_preview()
    await validate_proposal(
        db,
        proposal=proposal,
        tenant_id=deps.tenant_id,
        user_id=deps.user.user_id,
        verify_artifact=_planned is None,
        _planned=_planned,
    )
    if not read_existing:
        return None
    action = actions[0]
    lineage = result_projection_service.freeze_lineage(
        tenant=deps.tenant,
        agent_code="user_mgmt",
        tool_codes=["user.import_preview", "user.import_execute"],
        subject_refs=[proposal.subject_ref],
        data_scope_hash=deps.data_scope_hash,
        projection_dependency_message_ids=deps.projection_dependency_message_ids,
    )
    authorization = {
        "resolvedModelId": str(deps.resolved_model_id),
        "resolvedProviderId": str(deps.resolved_provider_id),
        "toolCodes": list(lineage.tool_codes),
        "subjectRefsHash": lineage.subject_refs_hash,
        "dataScopeHash": lineage.data_scope_hash,
        "resolverVersion": lineage.resolver_version,
        "projectionDependencyMessageIds": [
            str(value) for value in lineage.projection_dependency_message_ids
        ],
    }
    expected = {
        "confirmation_id": context.confirmation_id,
        "execute_tool_call_id": context.execute_tool_call_id,
        "execute_tool_name": "user.import_execute",
        "interaction_flow": "prepared",
        "requested_outcome": "execute_if_approved",
        "status": "pending_confirmation",
        "row_version": 1,
        "approval_mode": "hitl",
        "dispatch_mode": "inline",
        "approved_by": None,
        "approved_at": None,
        "finished_at": None,
        "error_code": None,
        "result_data": None,
        "result_ui": None,
        "duration_ms": None,
        "execution_owner": None,
        "execution_lease_expires_at": None,
        "tool_codes": list(lineage.tool_codes),
        "subject_refs": list(lineage.subject_refs),
        "subject_refs_hash": lineage.subject_refs_hash,
        "resolver_version": lineage.resolver_version,
        "projection_dependency_message_ids": authorization[
            "projectionDependencyMessageIds"
        ],
        "user_id": deps.user.user_id,
        "conversation_id": deps.conversation_id,
        "source_user_message_id": deps.source_user_message_id,
        "trace_id": deps.trace_id,
        "agent_code": "user_mgmt",
        "resolved_model_id": deps.resolved_model_id,
        "resolved_provider_id": deps.resolved_provider_id,
        "guard_owner_token": deps.guard_owner_token,
        "command_action": deps.command_action,
        "data_scope_hash": deps.data_scope_hash,
        "frozen_args": proposal.frozen_args,
        "subject_ref": proposal.subject_ref,
        "presentation": prepared_action_service.validate_presentation(
            proposal.presentation
        ),
    }
    if (
        any(getattr(action, key) != value for key, value in expected.items())
        or action.snapshot_hash != compute_args_hash(action.snapshot)
        or action.args_hash != child.args_hash
        or action.snapshot.get("_authorization") != authorization
        or {k: v for k, v in action.snapshot.items() if k != "_authorization"}
        != proposal.snapshot
        or child.target_summary != build_target_summary([proposal.subject_ref])
    ):
        raise invalid_preview()
    return action
