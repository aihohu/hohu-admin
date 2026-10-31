"""Import preview and optional confirmation handoff before the final FILE phase."""

import json
import re
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
from app.modules.ai.agents.gateway.result import (
    PreparedActionProposal,
    ResultProjection,
    ToolResult,
)
from app.modules.ai.agents.hitl.events import _ui_to_dict
from app.modules.ai.agents.tools.file_access import (
    IMPORT_MIME_TYPES_BY_EXTENSION,
    FileAccessPolicy,
    _not_found,
    read_protected_file,
)
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.authorization_chat_candidate import chat_candidate
from app.modules.ai.service.authorization_chat_model import recheck_model, select_model
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.authorization_execution_candidate import (
    ai_execution_candidate,
)
from app.modules.ai.service.operation_log_service import build_target_summary
from app.modules.ai.service.preview_completion_service import (
    preview_lineage,
    validate_context,
    verify_historical_preview,
)
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.system.ai_tools.user_transfer import _planned_preview_result
from app.modules.system.models.file import File
from app.modules.system.models.user_transfer import UserImportBatch
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


@dataclass(frozen=True, slots=True)
class GatewayPreviewBinding:
    log_id: int
    tool_call_id: str
    args_hash: str
    args_json: str | None
    scope_hash: str
    model_id: int
    provider_id: int

    def arguments(self):
        if (
            type(self.log_id) is not int
            or self.log_id <= 0
            or type(self.tool_call_id) is not str
            or not 1 <= len(self.tool_call_id) <= 64
            or self.args_json is not None
            and (
                type(self.args_json) is not str
                or len(self.args_json.encode()) > 256 * 1024
            )
            or type(self.args_hash) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.args_hash) is None
            or type(self.scope_hash) is not str
            or len(self.scope_hash) != 64
            or type(self.model_id) is not int
            or self.model_id <= 0
            or type(self.provider_id) is not int
            or self.provider_id <= 0
        ):
            raise _order_error()
        if self.args_json is None:
            return None
        args = json.loads(self.args_json)
        if (
            type(args) is not dict
            or set(args) - {"file_id", "reason", "on_conflict", "sync_mode"}
            or compute_args_hash(args) != self.args_hash
        ):
            raise _order_error()
        return args


async def _bound_log(db, binding, *, tenant, conversation, source, trace_id):
    binding.arguments()
    log = await db.get(AiOperationLog, binding.log_id, populate_existing=True)
    validate_context(
        log,
        conversation,
        source,
        tenant_id=tenant.tenant_id,
        user_id=tenant.actor_user_id,
    )
    if (
        log.tool_call_id != binding.tool_call_id
        or log.args_hash != binding.args_hash
        or log.trace_id != trace_id
        or not source.is_active
        or conversation.deleted_at is not None
        or await db.scalar(
            select(AiPreparedAction.action_id).where(
                AiPreparedAction.tenant_id == tenant.tenant_id,
                AiPreparedAction.execute_tool_call_id == binding.tool_call_id,
            )
        )
        is not None
    ):
        raise _order_error()
    return log


def _gateway_authority(binding, source):
    return SimpleNamespace(
        execute_tool_name="user.import_preview",
        interaction_flow="prepared",
        requested_outcome="execute_if_approved",
        approval_mode="required",
        dispatch_mode="execute_after_approval",
        agent_code="user_mgmt",
        data_scope_hash=binding.scope_hash,
        resolved_model_id=binding.model_id,
        resolved_provider_id=binding.provider_id,
        projection_dependency_message_ids=source.projection_dependency_message_ids,
    )


class PreviewCandidate:
    async def admit_gateway(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        conversation_id,
        source_id,
        trace_id,
        gateway,
    ):
        if type(gateway) is not GatewayPreviewBinding or gateway.arguments() is None:
            raise _order_error()
        selection = await select_model(db, tenant, str(gateway.model_id))
        agent_id = await db.scalar(
            select(AiAgent.agent_id).where(AiAgent.code == "user_mgmt")
        )
        if agent_id is None:
            raise _order_error()
        conversation, _actor = await chat_candidate._lock(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            conversation_id=conversation_id,
            extra=(*selection.keys, ResourceKey(K.AGENT, None, agent_id)),
        )
        source = await db.get(AiMessage, source_id, populate_existing=True)
        log = await _bound_log(
            db,
            gateway,
            tenant=tenant,
            conversation=conversation,
            source=source,
            trace_id=trace_id,
        )
        if (
            log.status != "running"
            or log.finished_at is not None
            or log.error_code is not None
        ):
            raise _order_error()
        await recheck_model(db, tx, tenant, selection)
        await ai_execution_candidate.authorize(
            db,
            tx,
            _gateway_authority(gateway, source),
            tenant=tenant,
            actor_user_version=actor_user_version,
            _preview=True,
        )

    async def receipt(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        conversation_id,
        source_id,
        trace_id,
        gateway,
    ):
        if type(gateway) is not GatewayPreviewBinding:
            raise _order_error()
        gateway.arguments()
        selection = await select_model(db, tenant, str(gateway.model_id))
        agent_id = await db.scalar(
            select(AiAgent.agent_id).where(AiAgent.code == "user_mgmt")
        )
        log = await db.get(AiOperationLog, gateway.log_id)
        if log is None or log.tenant_id != tenant.tenant_id:
            raise _order_error()
        try:
            refs = json.loads(log.target_summary)
            batch_id = refs[0]["id"]
            if type(batch_id) is not str or not 1 <= len(batch_id) <= 64:
                raise ValueError()
        except (TypeError, ValueError, IndexError, KeyError):
            raise _order_error() from None
        batch_query = select(UserImportBatch.batch_id).where(
            UserImportBatch.tenant_id == tenant.tenant_id,
            UserImportBatch.batch_id == batch_id,
        )
        existing_batch = await db.scalar(batch_query)
        if agent_id is None:
            raise _order_error()
        conversation, actor = await chat_candidate._lock(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            conversation_id=conversation_id,
            extra=(
                *selection.keys,
                ResourceKey(K.AGENT, None, agent_id),
                *(
                    (ResourceKey(K.IMPORT_BATCH, tenant.tenant_id, batch_id),)
                    if existing_batch is not None
                    else ()
                ),
            ),
        )
        if await db.scalar(batch_query) != existing_batch:
            raise _order_error()
        source = await db.get(AiMessage, source_id, populate_existing=True)
        log = await _bound_log(
            db,
            gateway,
            tenant=tenant,
            conversation=conversation,
            source=source,
            trace_id=trace_id,
        )
        await recheck_model(db, tx, tenant, selection)
        await ai_execution_candidate.authorize(
            db,
            tx,
            _gateway_authority(gateway, source),
            tenant=tenant,
            actor_user_version=actor_user_version,
            _preview=True,
        )
        audit = await verify_historical_preview(
            db,
            log,
            conversation,
            source,
            tenant_id=tenant.tenant_id,
            user_id=tenant.actor_user_id,
        )
        if audit["data_scope_hash"] != gateway.scope_hash:
            raise _order_error()
        visible = await result_projection_service.authorize_result_projection(
            db,
            actor,
            owner_user_id=tenant.actor_user_id,
            lineage=preview_lineage(log, audit, tenant),
        )
        data = {"batchId": audit["batch_id"]}
        if visible:
            data.update(total=audit["total"], summary=audit["summary"])
        return ToolResult.success(
            data,
            projection=ResultProjection(
                subject_refs=({"type": "user_import_batch", "id": audit["batch_id"]},),
                scope_bound=True,
            ),
            preview_replay=True,
        )

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
        gateway=None,
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
        selection = await select_model(
            db, tenant, str(gateway.model_id) if gateway else conversation.model_name
        )
        inventory = await ai_context_candidate.discover(
            db, tx, tenant_id=tenant.tenant_id, conversation_ids=(conversation_id,)
        )
        agent_id = await db.scalar(
            select(AiAgent.agent_id).where(AiAgent.code == "user_mgmt")
        )
        if agent_id is None:
            raise _order_error()
        parent = ResourceKey(K.CONVERSATION, tenant.tenant_id, conversation_id)
        if gateway is not None and type(gateway) is not GatewayPreviewBinding:
            raise _order_error()
        log_key = ResourceKey(
            K.OPERATION_LOG, tenant.tenant_id, gateway.log_id if gateway else next_id()
        )
        action_key = ResourceKey(K.PREPARED_ACTION, tenant.tenant_id, next_id())
        child_key = ResourceKey(K.OPERATION_LOG, tenant.tenant_id, next_id())
        handoff = gateway is None and requested_outcome == "execute_if_approved"
        new = (
            *work.locks.new,
            *((NewResource(log_key, parent),) if gateway is None else ()),
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
                    | {ResourceKey(K.AGENT, None, agent_id)}
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
        if gateway is not None:
            args = gateway.arguments()
            log = await _bound_log(
                db,
                gateway,
                tenant=tenant,
                conversation=conversation,
                source=source,
                trace_id=trace_id,
            )
            if (
                log.status != "running"
                or log.finished_at is not None
                or log.error_code is not None
                or scope_hash != gateway.scope_hash
                or selection.model_id != gateway.model_id
                or selection.provider_id != gateway.provider_id
                or command.execution.operation_id != gateway.tool_call_id
                or command.execution.execution_owner != f"preview:{gateway.log_id}"
                or args.get("file_id") != file_id
                or args.get("reason") != command.values[3]
                or args.get("on_conflict", "skip") != command.values[4]
                or args.get("sync_mode", "CREATE_ONLY") != sync_mode
            ):
                raise _order_error()
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
        if gateway is not None:
            authority = _gateway_authority(gateway, source)
        await ai_execution_candidate.authorize(
            db,
            tx,
            authority,
            tenant=tenant,
            actor_user_version=command.version,
            _preview=True,
        )
        batch, detail = await transfer_candidate.materialize_preview(
            db, tx, command, ctx.user, field_plan=work.field_plan
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
        if (
            not result.ok
            or not isinstance(proposal, PreparedActionProposal)
            or result.projection is None
            or tuple(result.projection.subject_refs) != (proposal.subject_ref,)
            or proposal.subject_ref
            != {"type": "user_import_batch", "id": command.resource_id}
            or result.data.get("batchId") != command.resource_id
        ):
            raise _order_error()
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
        if gateway is None:
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
            expected={
                "status": "running",
                "args_hash": gateway.args_hash if gateway else compute_args_hash(args),
            },
        )
        if gateway is not None:
            result.meta["execute_tool_call_id"] = "tc_" + uuid4().hex
            result.meta["confirmation_id"] = "cid_" + uuid4().hex
            return result
        return PreviewReference(command.resource_id, confirmation, call_id)


preview_candidate = PreviewCandidate()
