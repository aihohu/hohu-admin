"""Explicit pending decision/claim state transitions under complete proofs."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import AuthorizationException, BusinessException
from app.core.file_storage import ArtifactExecution
from app.core.id_generator import next_id
from app.db.authorization_plan_session import _order_error
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import (
    DB_TOOLS,
    FILE_TOOLS,
    ClaimBinding,
    _check_log,
    atomic_completion_service,
    is_protocol_owner,
    is_registered_readonly_action,
)
from app.modules.ai.service.authorization_chat_model import recheck_model, select_model
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.authorization_execution_candidate import (
    ai_execution_candidate,
)
from app.modules.ai.service.authorization_recovery_candidate import (
    ai_recovery_candidate,
)
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.reconciliation_annotation import has_annotation
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.ai.service.resume_terminal_projection import project_resume_terminal
from app.modules.auth.service import load_live_user_authority
from app.modules.system.service.authorization_ai_tool_adapter import (
    system_ai_tool_adapter,
)
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)

_EXECUTION_LEASE_TTL = timedelta(seconds=90)


@dataclass(frozen=True, slots=True, repr=False)
class SweepResult:
    action: object = None
    disposition: str = "unknown"
    changed: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class ConfirmationAdmission:
    action: object
    disposition: str
    changed: bool = False
    error: BusinessException | None = None


@dataclass(frozen=True, slots=True)
class ResumeBinding:
    action_id: int | None
    conversation_id: int
    source_id: int | None
    trace_id: str
    tool_call_id: str
    tool_name: str
    agent_code: str | None
    args_hash: str

    def require(self, action):
        if (
            (self.action_id is not None and self.action_id != action.action_id)
            or self.conversation_id != action.conversation_id
            or self.source_id != action.source_user_message_id
            or self.trace_id != action.trace_id
            or self.tool_call_id != action.execute_tool_call_id
            or self.tool_name != action.execute_tool_name
            or self.agent_code != action.agent_code
            or self.args_hash != action.args_hash
        ):
            raise _order_error()


class LifecycleCandidate:
    async def sweep(
        self, db, tx, confirmation_id, *, locator, startup, startup_before=None
    ):
        tx.assert_session(db)
        if type(startup) is not bool:
            raise _order_error()
        reference = await self.reference(db, confirmation_id, tenant=locator)
        if reference is None:
            return SweepResult()
        if (
            reference.status in {"running", "succeeded", "failed"}
            and reference.execute_tool_name in DB_TOOLS | FILE_TOOLS
        ):
            return await ai_recovery_candidate.reconcile(
                db, tx, confirmation_id, tenant=locator, allow_fence=True
            )
        readonly = is_registered_readonly_action(reference)
        if (
            reference.status not in {"pending_confirmation", "expired", "rejected"}
            and not readonly
        ):
            return SweepResult()
        action, conversation, source, log = await self._locked(
            db, tx, reference, locator, None
        )
        readonly = is_registered_readonly_action(action)
        if action.status in {"expired", "rejected", "failed", "succeeded"}:
            if readonly:
                self.verify_readonly_terminal(action, log)
            elif (
                action.status in {"expired", "rejected"}
                and action.execute_tool_name in DB_TOOLS | FILE_TOOLS
            ):
                atomic_completion_service.validate_locked_unexecuted_terminal(
                    action,
                    log,
                    tenant=locator,
                    conversation=conversation,
                    source=source,
                )
            else:
                return SweepResult()
            return SweepResult(action, action.status)
        if action.status not in {"pending_confirmation", "approved", "running"}:
            return SweepResult()
        if (
            action.error_code is not None
            or action.finished_at is not None
            or log.finished_at is not None
        ):
            return SweepResult()
        if readonly:
            expected = {
                "tenant_id": action.tenant_id,
                "user_id": action.user_id,
                "conversation_id": action.conversation_id,
                "source_user_message_id": action.source_user_message_id,
                "trace_id": action.trace_id,
                "agent_code": action.agent_code,
                "tool_name": action.execute_tool_name,
                "tool_call_id": action.execute_tool_call_id,
                "confirmation_id": action.confirmation_id,
                "args_hash": action.args_hash,
                "readonly_snapshot": True,
                "execution_mode": "hitl",
                "error_code": None,
            }
            if any(getattr(log, key) != value for key, value in expected.items()):
                return SweepResult()
        elif action.status == "pending_confirmation":
            _check_log(action, log, "pending_confirmation")
        else:
            return SweepResult()
        now = await tx.database_now()
        if action.status == "pending_confirmation":
            if (
                action.row_version != 1
                or action.execution_owner is not None
                or log.status != "pending_confirmation"
                or log.started_at is not None
            ):
                return SweepResult()
            if action.expires_at > now:
                if not startup or (
                    source.is_active and conversation.deleted_at is None
                ):
                    return SweepResult(action, "pending_confirmation")
                code = "AI_PREPARED_ACTION_SOURCE_STALE"
            else:
                code = "AI_HITL_EXPIRED"
            status = "expired"
        elif readonly and action.status == "approved" and startup:
            if startup_before is not None and (
                action.approved_at is None or action.approved_at > startup_before
            ):
                return SweepResult()
            if log.status not in {"pending_confirmation", "running"}:
                return SweepResult()
            status, code = "failed", "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
        elif readonly and action.status == "running":
            if (
                log.status != "running"
                or log.started_at is None
                or (
                    action.execution_lease_expires_at is not None
                    and action.execution_lease_expires_at > now
                )
            ):
                return SweepResult()
            status, code = "failed", "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
        else:
            return SweepResult()
        await tx.update_factual(
            ResourceKey(K.PREPARED_ACTION, locator.tenant_id, action.action_id),
            {
                "status": status,
                "row_version": action.row_version + 1,
                "finished_at": now,
                "error_code": code,
                "execution_owner": None,
                "execution_lease_expires_at": None,
                "duration_ms": 0,
            },
            expected={
                "status": action.status,
                "row_version": action.row_version,
                "execution_owner": action.execution_owner,
            },
        )
        await tx.update_factual(
            ResourceKey(K.OPERATION_LOG, locator.tenant_id, log.log_id),
            {
                "status": "expired"
                if log.status == "pending_confirmation"
                else "failed",
                "finished_at": max(
                    now.replace(tzinfo=None),
                    log.queued_at,
                    log.started_at or log.queued_at,
                ),
                "error_code": code,
                "duration_ms": log.duration_ms or 0,
            },
            expected={"status": log.status, "finished_at": None},
        )
        action = await self.reference(db, confirmation_id, tenant=locator)
        return SweepResult(action, status, True)

    @staticmethod
    def verify_readonly_terminal(action, log):
        statuses = {
            "expired": {"expired"},
            "failed": {"expired", "failed"},
            "succeeded": {"success"},
            "rejected": {"rejected"},
        }
        if (
            log is None
            or action.status not in statuses
            or not is_registered_readonly_action(action)
            or action.finished_at is None
            or action.row_version < 2
            or (action.status == "succeeded" and action.error_code is not None)
            or (action.status in {"expired", "failed"} and not action.error_code)
            or action.execution_owner is not None
            or action.execution_lease_expires_at is not None
            or compute_args_hash(action.frozen_args) != action.args_hash
            or compute_args_hash(action.snapshot) != action.snapshot_hash
            or log.status not in statuses[action.status]
            or log.finished_at is None
            or log.finished_at < log.queued_at
            or (log.started_at is not None and log.finished_at < log.started_at)
            or has_annotation(log.result_summary)
        ):
            raise _order_error()
        expected = {
            "tenant_id": action.tenant_id,
            "user_id": action.user_id,
            "conversation_id": action.conversation_id,
            "source_user_message_id": action.source_user_message_id,
            "trace_id": action.trace_id,
            "agent_code": action.agent_code,
            "tool_name": action.execute_tool_name,
            "confirmation_id": action.confirmation_id,
            "args_hash": action.args_hash,
            "readonly_snapshot": True,
            "execution_mode": "hitl",
            "error_code": action.error_code,
        }
        if any(getattr(log, key) != value for key, value in expected.items()):
            raise _order_error()

    async def prepare(
        self,
        db,
        tx,
        command,
        *,
        tenant,
        actor_user_version,
        tool,
        args,
        snapshot,
        conversation_id,
        source_id,
        trace_id,
        dry_run=False,
    ):
        tx.assert_planning(tenant.tenant_id, exclusive=True)
        if type(dry_run) is not bool:
            raise _order_error()
        registered = ToolRegistry.get().find(tool)
        if registered is None:
            raise _order_error()
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("ai:chat:use",),
        )
        actor = await load_live_user_authority(db, tenant=tenant)
        scope_hash = await result_projection_service.compute_data_scope_hash(db, actor)
        authorization_snapshot = (
            dict(snapshot) if tool == "user.import_execute" else {"business": snapshot}
        )
        action_id = (
            command.command.execution.operation_id
            if tool in {"user.export", "user.import_execute"}
            else str(next_id())
        )
        action = AiPreparedAction(
            action_id=int(action_id),
            tenant_id=tenant.tenant_id,
            user_id=tenant.actor_user_id,
            conversation_id=conversation_id,
            source_user_message_id=source_id,
            trace_id=trace_id,
            confirmation_id="cid_" + uuid4().hex,
            execute_tool_call_id="tc_" + uuid4().hex,
            execute_tool_name=tool,
            agent_code=registered.meta.agent,
            interaction_flow="prepared" if tool == "user.import_execute" else "direct",
            requested_outcome="execute_if_approved"
            if tool == "user.import_execute"
            else "direct",
            approval_mode="required",
            dispatch_mode="execute_after_approval",
            data_scope_hash=scope_hash,
            projection_dependency_message_ids=[],
        )
        await ai_execution_candidate.authorize(
            db, tx, action, tenant=tenant, actor_user_version=actor_user_version
        )
        system_ai_tool_adapter.validate_binding(
            command,
            tool=tool,
            args=args,
            snapshot=snapshot,
            tenant=tenant,
            actor_user_version=actor_user_version,
        )
        work = await system_ai_tool_adapter.plan(db, tx, command)
        inventory = await ai_context_candidate.discover(
            db, tx, tenant_id=tenant.tenant_id, conversation_ids=(conversation_id,)
        )
        conversation = await db.get(
            AiConversation, conversation_id, populate_existing=True
        )
        if conversation is None or conversation.tenant_id != tenant.tenant_id:
            raise _order_error()
        model_selection = await select_model(db, tenant, conversation.model_name)
        parent = ResourceKey(K.CONVERSATION, tenant.tenant_id, conversation_id)
        action_key = ResourceKey(K.PREPARED_ACTION, tenant.tenant_id, int(action_id))
        log_key = ResourceKey(K.OPERATION_LOG, tenant.tenant_id, next_id())
        await tx.lock(
            LockPlan(
                existing=tuple(
                    set(inventory.keys)
                    | set(identity.keys)
                    | set(work.locks.existing)
                    | set(model_selection.keys)
                ),
                new=(
                    *work.locks.new,
                    NewResource(action_key, parent),
                    NewResource(log_key, parent),
                ),
            )
        )
        await ai_context_candidate.recheck(db, tx, inventory)
        await assert_identity(db, identity)
        await recheck_model(db, tx, tenant, model_selection)
        conversation = await db.get(
            AiConversation, conversation_id, populate_existing=True
        )
        source = await db.get(AiMessage, source_id, populate_existing=True)
        if (
            conversation is None
            or conversation.tenant_id != tenant.tenant_id
            or conversation.user_id != tenant.actor_user_id
            or conversation.deleted_at is not None
            or source is None
            or source.tenant_id != tenant.tenant_id
            or source.conversation_id != conversation_id
            or source.role != "user"
            or not source.is_active
            or source.trace_id != trace_id
        ):
            raise _order_error()
        await system_ai_tool_adapter.validate_planned(db, tx, command, work)
        await ai_execution_candidate.authorize(
            db, tx, action, tenant=tenant, actor_user_version=actor_user_version
        )
        if dry_run:
            return None
        refs = prepared_action_service._build_subject_refs(
            execute_tool_name=tool,
            frozen_args=args,
            snapshot=authorization_snapshot,
            subject_ref=None,
            projection_kind=registered.meta.projection_kind,
        )
        dependencies = result_projection_service.projection_dependency_message_ids(
            source
        )
        if (
            dependencies is None
            or not await result_projection_service._authorize_projection_dependencies(
                db,
                actor,
                owner_user_id=tenant.actor_user_id,
                dependency_message_ids=dependencies,
            )
        ):
            raise _order_error()
        lineage = result_projection_service.freeze_lineage(
            tenant=tenant,
            agent_code=action.agent_code,
            tool_codes=[tool],
            subject_refs=refs,
            data_scope_hash=scope_hash,
            projection_dependency_message_ids=dependencies,
        )
        now = await tx.database_now()
        args_hash = compute_args_hash(args)
        values = {
            "confirmation_id": action.confirmation_id,
            "execute_tool_call_id": action.execute_tool_call_id,
            "execute_tool_name": tool,
            "tenant_id": tenant.tenant_id,
            "user_id": tenant.actor_user_id,
            "conversation_id": conversation_id,
            "source_user_message_id": source_id,
            "trace_id": trace_id,
            "agent_code": action.agent_code,
            "interaction_flow": action.interaction_flow,
            "requested_outcome": action.requested_outcome,
            "approval_mode": "required",
            "dispatch_mode": "execute_after_approval",
            "status": "pending_confirmation",
            "row_version": 1,
            "frozen_args": args,
            "args_hash": args_hash,
            "snapshot": authorization_snapshot,
            "snapshot_hash": compute_args_hash(authorization_snapshot),
            "presentation": {"title": tool},
            "tool_codes": list(lineage.tool_codes),
            "subject_refs": list(lineage.subject_refs),
            "subject_refs_hash": lineage.subject_refs_hash,
            "data_scope_hash": lineage.data_scope_hash,
            "resolver_version": lineage.resolver_version,
            "projection_dependency_message_ids": [],
            "expires_at": now + timedelta(minutes=5),
            "risk_level": registered.meta.risk,
        }
        values.update(
            resolved_model_id=model_selection.model_id,
            resolved_provider_id=model_selection.provider_id,
            projection_dependency_message_ids=[str(v) for v in dependencies],
        )
        await tx.insert_reserved(action_key, values)
        await tx.insert_reserved(
            log_key,
            {
                "conversation_id": conversation_id,
                "source_user_message_id": source_id,
                "user_id": tenant.actor_user_id,
                "trace_id": trace_id,
                "agent_code": action.agent_code,
                "tool_name": tool,
                "tool_call_id": action.execute_tool_call_id,
                "args_hash": args_hash,
                "args_summary": "tool=" + tool,
                "risk_level": registered.meta.risk,
                "execution_mode": "hitl",
                "status": "pending_confirmation",
                "confirmation_id": action.confirmation_id,
                "readonly_snapshot": False,
                "queued_at": now.replace(tzinfo=None),
            },
        )
        return action.confirmation_id

    async def reference(self, db, confirmation_id, *, tenant):
        return await db.scalar(
            select(AiPreparedAction)
            .where(
                AiPreparedAction.tenant_id == tenant.tenant_id,
                AiPreparedAction.user_id == tenant.actor_user_id,
                AiPreparedAction.confirmation_id == confirmation_id,
            )
            .execution_options(populate_existing=True)
        )

    async def _locked(self, db, tx, reference, tenant, identity, work=None):
        inventory = await ai_context_candidate.discover(
            db,
            tx,
            tenant_id=tenant.tenant_id,
            conversation_ids=(reference.conversation_id,),
        )
        keys = set(inventory.keys) | (
            set(identity.keys) if identity is not None else set()
        )
        if work is not None:
            keys.update(work.locks.existing)
        await tx.lock(
            LockPlan(existing=tuple(keys), new=work.locks.new if work else ())
        )
        await ai_context_candidate.recheck(db, tx, inventory)
        if identity is not None:
            await assert_identity(db, identity)
        action = await self.reference(db, reference.confirmation_id, tenant=tenant)
        if action is None or action.action_id != reference.action_id:
            raise _order_error()
        conversation = await db.get(
            AiConversation, action.conversation_id, populate_existing=True
        )
        source = await db.get(
            AiMessage, action.source_user_message_id, populate_existing=True
        )
        log = await db.scalar(
            select(AiOperationLog)
            .where(
                AiOperationLog.tenant_id == tenant.tenant_id,
                AiOperationLog.tool_call_id == action.execute_tool_call_id,
            )
            .execution_options(populate_existing=True)
        )
        if (
            conversation is None
            or conversation.tenant_id != tenant.tenant_id
            or conversation.user_id != tenant.actor_user_id
            or source is None
            or source.tenant_id != tenant.tenant_id
            or source.conversation_id != action.conversation_id
            or source.role != "user"
            or source.trace_id != action.trace_id
            or log is None
        ):
            raise _order_error()
        if action.args_hash != compute_args_hash(
            action.frozen_args
        ) or action.snapshot_hash != compute_args_hash(action.snapshot):
            raise _order_error()
        tx.assert_held(
            ResourceKey(K.PREPARED_ACTION, tenant.tenant_id, action.action_id),
            ResourceKey(K.OPERATION_LOG, tenant.tenant_id, log.log_id),
        )
        return action, conversation, source, log

    async def admit(
        self,
        db,
        tx,
        confirmation_id,
        *,
        tenant,
        actor_user_version,
        decision,
        disabled,
        denial=None,
        resume_binding=None,
    ):
        if (
            decision not in {"approve", "reject"}
            or (
                resume_binding is not None and type(resume_binding) is not ResumeBinding
            )
            or type(disabled) is not bool
            or (
                denial is not None
                and (
                    not isinstance(denial, BusinessException)
                    or not 400 <= denial.code < 500
                    or decision != "approve"
                    or denial.error_code.startswith("AUTHORIZATION_")
                )
            )
        ):
            raise _order_error()
        identity = await read_identity(
            db, tenant=tenant, actor_user_version=actor_user_version
        )
        reference = await self.reference(db, confirmation_id, tenant=tenant)
        if reference is None:
            raise _order_error()
        if resume_binding is not None:
            resume_binding.require(reference)
        # Admission only decides pending work. A terminal or running reference
        # must pass the separate completion proof, which also handles missing
        # historical context conservatively without attempting another claim.
        if reference.status != "pending_confirmation":
            return ConfirmationAdmission(reference, reference.status)
        action, conversation, source, log = await self._locked(
            db, tx, reference, tenant, identity
        )
        if resume_binding is not None:
            resume_binding.require(action)
        if has_annotation(log.result_summary):
            return ConfirmationAdmission(action, "unknown")
        if action.status != "pending_confirmation":
            return ConfirmationAdmission(action, action.status)
        _check_log(action, log, "pending_confirmation")
        now = await tx.database_now()
        error = None
        if action.expires_at <= now:
            finish = "sweep_expire"
        elif conversation.deleted_at is not None or not source.is_active:
            finish = "source_stale"
        elif decision == "reject":
            finish = "reject"
        elif denial is not None:
            error, finish = denial, "denied"
        else:
            finish = None
            try:
                if disabled:
                    raise AuthorizationException(
                        "AI 已被禁用，无法确认操作", error_code="AI_USER_DISABLED"
                    )
                await ai_execution_candidate.authorize(
                    db, tx, action, tenant=tenant, actor_user_version=actor_user_version
                )
            except BusinessException as exc:
                if not 400 <= exc.code < 500 or exc.error_code.startswith(
                    "AUTHORIZATION_"
                ):
                    raise
                error, finish = exc, "denied"
        if finish is None:
            return ConfirmationAdmission(action, "pending_confirmation")
        status = await self._finish_decision(
            tx, tenant, action, log, finish, error=error
        )
        action = await self.reference(db, confirmation_id, tenant=tenant)
        return ConfirmationAdmission(action, status, True, error)

    async def unexecuted(self, db, tx, confirmation_id, *, tenant):
        tx.assert_session(db)
        reference = await self.reference(db, confirmation_id, tenant=tenant)
        if reference is None:
            raise _order_error()
        action, conversation, source, log = await self._locked(
            db, tx, reference, tenant, None
        )
        atomic_completion_service.validate_locked_unexecuted_terminal(
            action, log, tenant=tenant, conversation=conversation, source=source
        )
        return action

    async def resume_read(
        self, db, tx, confirmation_id, *, tenant, actor_user_version, resume_binding
    ):
        if type(resume_binding) is not ResumeBinding:
            raise _order_error()
        identity = await read_identity(
            db, tenant=tenant, actor_user_version=actor_user_version
        )
        reference = await self.reference(db, confirmation_id, tenant=tenant)
        if reference is None:
            raise _order_error()
        resume_binding.require(reference)
        action, conversation, source, log = await self._locked(
            db, tx, reference, tenant, identity
        )
        resume_binding.require(action)
        if (
            action.execute_tool_name not in DB_TOOLS | FILE_TOOLS
            or action.status not in {"succeeded", "failed", "expired", "rejected"}
            or (action.execute_tool_name in FILE_TOOLS and action.status == "failed")
        ):
            raise _order_error()
        validator = (
            atomic_completion_service.validate_locked_unexecuted_terminal
            if action.status in {"expired", "rejected"}
            else atomic_completion_service.validate_locked_terminal
        )
        validator(action, log, tenant=tenant, conversation=conversation, source=source)
        actor = await load_live_user_authority(db, tenant=tenant)
        return await project_resume_terminal(db, action, actor, tenant)

    async def gateway_timeout(
        self, db, tx, confirmation_id, *, tenant, actor_user_version, resume_binding
    ):
        if type(resume_binding) is not ResumeBinding:
            raise _order_error()
        identity = await read_identity(
            db, tenant=tenant, actor_user_version=actor_user_version
        )
        reference = await self.reference(db, confirmation_id, tenant=tenant)
        if reference is None:
            raise _order_error()
        resume_binding.require(reference)
        action, conversation, source, log = await self._locked(
            db, tx, reference, tenant, identity
        )
        resume_binding.require(action)
        if action.status in {"expired", "rejected"}:
            atomic_completion_service.validate_locked_unexecuted_terminal(
                action, log, tenant=tenant, conversation=conversation, source=source
            )
            return action.status
        if action.status in {"succeeded", "failed"}:
            # The caller still needs a separate currently authorized projection.
            return action.status
        if (
            action.status != "pending_confirmation"
            or action.expires_at > await tx.database_now()
        ):
            raise _order_error()
        return await self._finish_decision(tx, tenant, action, log, "sweep_expire")

    async def confirm(
        self, db, tx, *, tenant, actor_user_version, confirmation_id, command, owner
    ):
        tx.assert_planning(tenant.tenant_id, exclusive=True)
        reference = await self.reference(db, confirmation_id, tenant=tenant)
        if reference is None:
            raise _order_error()
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("ai:chat:use",),
        )
        await ai_execution_candidate.authorize(
            db, tx, reference, tenant=tenant, actor_user_version=actor_user_version
        )
        snapshot = (
            {k: v for k, v in reference.snapshot.items() if k != "_authorization"}
            if reference.execute_tool_name == "user.import_execute"
            else reference.snapshot.get("business")
        )
        system_ai_tool_adapter.validate_binding(
            command,
            tool=reference.execute_tool_name,
            args=reference.frozen_args,
            snapshot=snapshot,
            tenant=tenant,
            actor_user_version=actor_user_version,
        )
        work = await system_ai_tool_adapter.plan(db, tx, command)
        action, conversation, source, log = await self._locked(
            db, tx, reference, tenant, identity, work
        )
        system_ai_tool_adapter.validate_binding(
            command,
            tool=action.execute_tool_name,
            args=action.frozen_args,
            snapshot=(
                {k: v for k, v in action.snapshot.items() if k != "_authorization"}
                if action.execute_tool_name == "user.import_execute"
                else action.snapshot.get("business")
            ),
            tenant=tenant,
            actor_user_version=actor_user_version,
        )
        if not is_protocol_owner(owner, action.execute_tool_name) or (
            action.execute_tool_name in {"user.export", "user.import_execute"}
            and command.command.execution
            != ArtifactExecution(tenant.tenant_id, str(action.action_id), owner)
        ):
            raise _order_error()
        now = await tx.database_now()
        if (
            action.status != "pending_confirmation"
            or action.row_version != 1
            or action.execution_owner is not None
            or action.expires_at <= now
            or conversation.deleted_at is not None
            or not source.is_active
        ):
            raise _order_error()
        _check_log(action, log, "pending_confirmation")
        await system_ai_tool_adapter.validate_planned(db, tx, command, work)
        await ai_execution_candidate.authorize(
            db, tx, action, tenant=tenant, actor_user_version=actor_user_version
        )
        key = ResourceKey(K.PREPARED_ACTION, tenant.tenant_id, action.action_id)
        await tx.update_factual(
            key,
            {
                "status": "approved",
                "row_version": 2,
                "approved_by": tenant.actor_user_id,
                "approved_at": now,
            },
            expected={
                "status": "pending_confirmation",
                "row_version": 1,
                "execution_owner": None,
                "args_hash": command.args_hash,
            },
        )
        await tx.update_factual(
            key,
            {
                "status": "running",
                "row_version": 3,
                "execution_owner": owner,
                "execution_lease_expires_at": now + _EXECUTION_LEASE_TTL,
            },
            expected={
                "status": "approved",
                "row_version": 2,
                "approved_by": tenant.actor_user_id,
            },
        )
        await tx.update_factual(
            ResourceKey(K.OPERATION_LOG, tenant.tenant_id, log.log_id),
            {
                "status": "running",
                "started_at": max(now.replace(tzinfo=None), log.queued_at),
                "approved_by": tenant.actor_user_id,
            },
            expected={
                "status": "pending_confirmation",
                "started_at": None,
                "finished_at": None,
            },
        )
        action = await self.reference(db, confirmation_id, tenant=tenant)
        return ClaimBinding.from_action(action, log.log_id)

    async def decide(
        self, db, tx, *, tenant, actor_user_version, confirmation_id, decision
    ):
        if decision not in {"reject", "cancel", "expire"}:
            raise _order_error()
        reference = await self.reference(db, confirmation_id, tenant=tenant)
        if reference is None:
            raise _order_error()
        identity = await read_identity(
            db, tenant=tenant, actor_user_version=actor_user_version
        )
        action, _, _, log = await self._locked(db, tx, reference, tenant, identity)
        return await self._finish_decision(tx, tenant, action, log, decision)

    async def expire(self, db, tx, confirmation_id, *, locator, startup=False):
        tx.assert_session(db)
        if type(startup) is not bool:
            raise _order_error()
        reference = await self.reference(db, confirmation_id, tenant=locator)
        if reference is None:
            raise _order_error()
        action, _, _, log = await self._locked(db, tx, reference, locator, None)
        if (
            action.status != "pending_confirmation"
            or action.row_version != 1
            or action.execution_owner is not None
        ):
            raise _order_error()
        _check_log(action, log, "pending_confirmation")
        now = await tx.database_now()
        if action.expires_at > now:
            if not startup or await prepared_action_service.pending_source_is_valid(
                db, action, tenant=locator
            ):
                return "pending_confirmation"
            decision = "source_stale"
        else:
            decision = "sweep_expire"
        return await self._finish_decision(tx, locator, action, log, decision)

    async def _finish_decision(self, tx, tenant, action, log, decision, *, error=None):
        now = await tx.database_now()
        if (
            action.status != "pending_confirmation"
            or action.row_version != 1
            or action.execution_owner is not None
            or (decision == "expire" and action.expires_at > now)
        ):
            raise _order_error()
        _check_log(action, log, "pending_confirmation")
        rejected = decision == "reject"
        status = "rejected" if rejected else "expired"
        code = {
            "reject": "USER_REJECTED",
            "expire": "AI_PREPARED_ACTION_EXPIRED",
            "startup": "AI_PREPARED_ACTION_INTERRUPTED",
            "source_stale": "AI_PREPARED_ACTION_SOURCE_STALE",
            "sweep_expire": "AI_HITL_EXPIRED",
        }.get(decision, "AI_PREPARED_ACTION_CANCELLED")
        if error is not None:
            if decision != "denied" or not isinstance(error, BusinessException):
                raise _order_error()
            code = error.error_code or "AI_PREPARED_ACTION_REVALIDATION_FAILED"
        approved_by = tenant.actor_user_id if rejected else None
        await tx.update_factual(
            ResourceKey(K.PREPARED_ACTION, tenant.tenant_id, action.action_id),
            {
                "status": status,
                "row_version": 2,
                "error_code": code,
                "finished_at": now,
                "approved_by": approved_by,
                "approved_at": now if rejected else None,
                "duration_ms": 0,
            },
            expected={
                "status": "pending_confirmation",
                "row_version": 1,
                "execution_owner": None,
            },
        )
        await tx.update_factual(
            ResourceKey(K.OPERATION_LOG, tenant.tenant_id, log.log_id),
            {
                "status": status,
                "error_code": code,
                "finished_at": max(now.replace(tzinfo=None), log.queued_at),
                "approved_by": approved_by,
                "duration_ms": 0,
            },
            expected={
                "status": "pending_confirmation",
                "started_at": None,
                "finished_at": None,
            },
        )
        return status


lifecycle_candidate = LifecycleCandidate()
