"""Import preview and optional confirmation handoff before the final FILE phase."""

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.id_generator import next_id
from app.db.authorization_plan_session import _order_error
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.hitl.events import _ui_to_dict
from app.modules.ai.agents.tools.file_access import (
    IMPORT_MIME_TYPES_BY_EXTENSION,
    FileAccessPolicy,
    _not_found,
    read_protected_file,
)
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.service.authorization_chat_model import recheck_model, select_model
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.authorization_execution_candidate import (
    ai_execution_candidate,
)
from app.modules.ai.service.operation_log_service import build_target_summary
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.system.ai_tools.user_transfer import _planned_preview_result
from app.modules.system.models.file import File
from app.modules.system.service.authorization_ai_user_input import context
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.authorization_transfer_candidate import (
    transfer_candidate,
)
from app.modules.system.service.file_policy_service import (
    UploadPolicy,
    file_policy_service,
)
from app.utils.attachment_filename import attachment_display_name


@dataclass(frozen=True, slots=True)
class PreviewInput:
    facts: tuple
    policy: UploadPolicy


def read_preview_input(snapshot, private_root):
    """Worker receives immutable metadata only, never a live ORM/session."""
    if type(snapshot) is not PreviewInput:
        raise _order_error()
    record = SimpleNamespace(**dict(snapshot.facts))
    protected = read_protected_file(
        record,
        policy=FileAccessPolicy(
            allowed_business_types=frozenset({"user-import"}),
            mime_types_by_extension=IMPORT_MIME_TYPES_BY_EXTENSION,
            max_bytes=10 * 1024 * 1024,
            storage_root_resolver=lambda _: Path(private_root),
        ),
        tenant_policy=snapshot.policy,
    )
    original = attachment_display_name(record.original_name)
    filename = (
        original
        if original
        and PurePosixPath(original).suffix.lower() == protected.path.suffix.lower()
        else protected.path.name
    )
    return protected.data, filename


@dataclass(frozen=True, slots=True)
class PreviewReference:
    batch_id: str
    confirmation_id: str | None
    tool_call_id: str


class PreviewCandidate:
    async def input_file(
        self, db, tx, *, tenant, actor_user_version, file_id, expected=None
    ):
        try:
            identifier = int(file_id)
        except (TypeError, ValueError):
            raise _not_found() from None
        if isinstance(file_id, bool) or not 0 < identifier < 2**63:
            raise _not_found()
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("ai:chat:use", "system:user:import"),
        )
        statement = select(File.__table__).where(
            File.tenant_id == tenant.tenant_id, File.file_id == identifier
        )
        row = (await db.execute(statement)).mappings().one_or_none()
        if (
            row is None
            or row["del_flag"] != "0"
            or row["owner_user_id"] != tenant.actor_user_id
        ):
            raise _not_found()
        await tx.lock(
            LockPlan(
                existing=(
                    *identity.keys,
                    ResourceKey(K.FILE, tenant.tenant_id, identifier),
                )
            )
        )
        await assert_identity(db, identity)
        current = (await db.execute(statement)).mappings().one_or_none()
        if current is None or dict(row) != dict(current):
            raise _order_error()
        snapshot = PreviewInput(
            tuple(sorted(current.items())),
            await file_policy_service.resolve(db, "ai_file", tenant=tenant),
        )
        if expected is not None and (
            type(expected) is not PreviewInput or expected != snapshot
        ):
            raise _order_error()
        return snapshot

    async def execute(
        self,
        db,
        tx,
        command,
        *,
        file_snapshot,
        file_policy,
        file_id,
        requested_outcome,
        sync_mode,
        conversation_id,
        source_id,
        trace_id,
        storage,
    ):
        tenant = command.tenant
        if requested_outcome not in {
            "preview_only",
            "execute_if_approved",
        } or sync_mode not in {"CREATE_ONLY", "UPDATE_PROFILE", "FULL_SYNC"}:
            raise _order_error()
        work = await transfer_candidate.plan(db, tx, command)
        conversation = await db.get(
            AiConversation, conversation_id, populate_existing=True
        )
        if conversation is None or conversation.tenant_id != tenant.tenant_id:
            raise _order_error()
        selection = await select_model(db, tenant, conversation.model_name)
        inventory = await ai_context_candidate.discover(
            db, tx, tenant_id=tenant.tenant_id, conversation_ids=(conversation_id,)
        )
        parent = ResourceKey(K.CONVERSATION, tenant.tenant_id, conversation_id)
        log_key = ResourceKey(K.OPERATION_LOG, tenant.tenant_id, next_id())
        action_key = ResourceKey(K.PREPARED_ACTION, tenant.tenant_id, next_id())
        child_key = ResourceKey(K.OPERATION_LOG, tenant.tenant_id, next_id())
        handoff = requested_outcome == "execute_if_approved"
        new = (
            *work.locks.new,
            NewResource(log_key, parent),
            *(
                (NewResource(action_key, parent), NewResource(child_key, parent))
                if handoff
                else ()
            ),
        )
        await tx.lock(
            LockPlan(
                existing=tuple(
                    set(inventory.keys)
                    | set(work.locks.existing)
                    | set(selection.keys)
                    | {ResourceKey(K.FILE, tenant.tenant_id, int(file_id))}
                ),
                new=new,
            )
        )
        await ai_context_candidate.recheck(db, tx, inventory)
        await assert_identity(db, work.identity)
        await recheck_model(db, tx, tenant, selection)
        record = (
            (
                await db.execute(
                    select(File.__table__).where(
                        File.tenant_id == tenant.tenant_id, File.file_id == int(file_id)
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if record is None or dict(record) != file_snapshot:
            raise _order_error()
        if (
            await file_policy_service.resolve(db, "ai_file", tenant=tenant)
            != file_policy
        ):
            raise _order_error()
        conversation = await db.get(
            AiConversation, conversation_id, populate_existing=True
        )
        source = await db.get(AiMessage, source_id, populate_existing=True)
        if (
            conversation is None
            or conversation.user_id != tenant.actor_user_id
            or conversation.tenant_id != tenant.tenant_id
            or conversation.deleted_at is not None
            or source is None
            or source.tenant_id != tenant.tenant_id
            or source.conversation_id != conversation_id
            or source.trace_id != trace_id
            or source.role != "user"
            or not source.is_active
        ):
            raise _order_error()
        ctx = await context(db, tenant, "user.import_preview")
        scope_hash = await result_projection_service.compute_data_scope_hash(
            db, ctx.user
        )
        authority = SimpleNamespace(
            execute_tool_name="user.import_preview",
            interaction_flow="prepared",
            requested_outcome="execute_if_approved",
            approval_mode="required",
            dispatch_mode="execute_after_approval",
            agent_code="user_mgmt",
            data_scope_hash=scope_hash,
            projection_dependency_message_ids=source.projection_dependency_message_ids,
        )
        await ai_execution_candidate.authorize(
            db,
            tx,
            authority,
            tenant=tenant,
            actor_user_version=command.version,
            _preview=True,
        )
        batch, detail = await transfer_candidate.materialize_preview(
            db, tx, command, ctx.user
        )
        result = _planned_preview_result(
            detail,
            SimpleNamespace(**batch),
            command.values[3],
            command.values[4],
            sync_mode,
            operator_id=tenant.actor_user_id,
        )
        proposal = result.prepared_action
        dependencies = result_projection_service.projection_dependency_message_ids(
            source
        )
        if dependencies is None:
            raise _order_error()
        call_id = command.execution.operation_id
        now = await tx.database_now()
        common = {
            "conversation_id": conversation_id,
            "source_user_message_id": source_id,
            "user_id": tenant.actor_user_id,
            "trace_id": trace_id,
            "agent_code": "user_mgmt",
            "readonly_snapshot": False,
            "queued_at": now.replace(tzinfo=None),
            "execution_mode": "hitl",
        }
        args = {
            "file_id": file_id,
            "reason": command.values[3],
            "on_conflict": command.values[4],
            "sync_mode": sync_mode,
            "requested_outcome": requested_outcome,
        }
        await tx.insert_reserved(
            log_key,
            dict(
                **common,
                tool_name="user.import_preview",
                tool_call_id=call_id,
                args_hash=compute_args_hash(args),
                args_summary="tool=user.import_preview",
                risk_level="low",
                status="running",
                started_at=now.replace(tzinfo=None),
            ),
        )
        confirmation = None
        if handoff:
            confirmation = "cid_" + uuid4().hex
            child_call = "tc_" + uuid4().hex
            lineage = result_projection_service.freeze_lineage(
                tenant=tenant,
                agent_code="user_mgmt",
                tool_codes=["user.import_preview", "user.import_execute"],
                subject_refs=[proposal.subject_ref],
                data_scope_hash=scope_hash,
                projection_dependency_message_ids=dependencies,
            )
            await tx.insert_reserved(
                action_key,
                {
                    "confirmation_id": confirmation,
                    "prepare_tool_call_id": call_id,
                    "execute_tool_call_id": child_call,
                    "execute_tool_name": "user.import_execute",
                    "user_id": tenant.actor_user_id,
                    "conversation_id": conversation_id,
                    "source_user_message_id": source_id,
                    "trace_id": trace_id,
                    "agent_code": "user_mgmt",
                    "interaction_flow": "prepared",
                    "requested_outcome": "execute_if_approved",
                    "approval_mode": "required",
                    "dispatch_mode": "execute_after_approval",
                    "status": "pending_confirmation",
                    "row_version": 1,
                    "frozen_args": proposal.frozen_args,
                    "args_hash": compute_args_hash(proposal.frozen_args),
                    "snapshot": proposal.snapshot,
                    "snapshot_hash": compute_args_hash(proposal.snapshot),
                    "subject_ref": proposal.subject_ref,
                    "presentation": proposal.presentation,
                    "tool_codes": list(lineage.tool_codes),
                    "subject_refs": list(lineage.subject_refs),
                    "subject_refs_hash": lineage.subject_refs_hash,
                    "data_scope_hash": scope_hash,
                    "resolver_version": lineage.resolver_version,
                    "projection_dependency_message_ids": [str(v) for v in dependencies],
                    "resolved_model_id": selection.model_id,
                    "resolved_provider_id": selection.provider_id,
                    "expires_at": proposal.expires_at,
                    "risk_level": "high",
                },
            )
            await tx.insert_reserved(
                child_key,
                dict(
                    **common,
                    tool_name="user.import_execute",
                    tool_call_id=child_call,
                    args_hash=compute_args_hash(proposal.frozen_args),
                    args_summary="tool=user.import_execute",
                    risk_level="high",
                    status="pending_confirmation",
                    confirmation_id=confirmation,
                ),
            )
        await transfer_candidate.publish_preview(
            db, tx, command, storage, batch, source_context=ctx, source_file_id=file_id
        )
        audit = {
            **proposal.snapshot,
            "on_conflict": command.values[4],
            "preview_data": result.data,
            "preview_ui": _ui_to_dict(result.ui),
            "data_scope_hash": scope_hash,
            "projection_dependency_message_ids": list(dependencies),
            "proposal_hash": compute_args_hash(
                {
                    "args": proposal.frozen_args,
                    "snapshot": proposal.snapshot,
                    "presentation": proposal.presentation,
                }
            ),
        }
        await tx.update_factual(
            log_key,
            {
                "status": "success",
                "finished_at": (await tx.database_now()).replace(tzinfo=None),
                "duration_ms": 0,
                "result_summary": json.dumps(
                    {"status": "success", "duration_ms": 0, "audit": audit},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "target_summary": build_target_summary([proposal.subject_ref]),
            },
            expected={"status": "running", "args_hash": compute_args_hash(args)},
        )
        return PreviewReference(command.resource_id, confirmation, call_id)


preview_candidate = PreviewCandidate()
