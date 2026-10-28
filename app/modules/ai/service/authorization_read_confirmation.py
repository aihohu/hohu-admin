"""Atomic forced confirmation of the fixed builtin database read tools."""

import json
import time
from dataclasses import asdict
from types import SimpleNamespace

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import AuthorizationException
from app.db.authorization_plan_session import _order_error
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.service.authorization_chat_model import recheck_model, select_model
from app.modules.ai.service.authorization_gateway_read_candidate import (
    READ_HANDLERS,
    gateway_read_candidate,
)
from app.modules.ai.service.authorization_lifecycle_candidate import lifecycle_candidate
from app.modules.ai.service.reconciliation_annotation import has_annotation
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.ai.service.resume_terminal_projection import project_resume_terminal
from app.modules.auth.service import load_live_user_authority
from app.modules.system.service.authorization_control_identity import read_identity


def read_terminal_marker(action):
    """Bind the new protocol to complete result and lineage, never old labels."""
    return "planned-read-v1:" + compute_args_hash(
        {
            "action": str(action.action_id),
            "call": action.execute_tool_call_id,
            "args": action.args_hash,
            "snapshot": action.snapshot_hash,
            "status": action.status,
            "version": action.row_version,
            "result": action.result_data,
            "ui": action.result_ui,
            "tools": action.tool_codes,
            "subjects": action.subject_refs,
            "subjectsHash": action.subject_refs_hash,
            "scope": action.data_scope_hash,
            "resolver": action.resolver_version,
            "dependencies": action.projection_dependency_message_ids,
        }
    )


def verify_read_terminal(action, log):
    lifecycle_candidate.verify_readonly_terminal(action, log)
    if action.status == "succeeded" and (
        action.row_version != 4
        or log.tool_call_id != action.execute_tool_call_id
        or log.approved_by != action.user_id
        or action.approved_by != action.user_id
        or action.approved_at is None
        or log.started_at is None
        or log.result_summary != read_terminal_marker(action)
    ):
        raise _order_error()


class ReadConfirmationCandidate:
    async def execute(
        self,
        db,
        tx,
        confirmation_id,
        *,
        tenant,
        actor_user_version,
        decision,
        disabled=False,
        binding=None,
    ):
        tx.assert_session(db)
        if (
            decision not in {"approve", "reject", "read", "timeout", "abort"}
            or type(disabled) is not bool
        ):
            raise _order_error()
        identity = await read_identity(
            db, tenant=tenant, actor_user_version=actor_user_version, permissions=()
        )
        reference = await lifecycle_candidate.reference(
            db, confirmation_id, tenant=tenant
        )
        if reference is None or reference.execute_tool_name not in READ_HANDLERS:
            raise _order_error()
        selection = None
        keys = ()
        if decision == "approve" and reference.status == "pending_confirmation":
            selection = await select_model(db, tenant, str(reference.resolved_model_id))
            agent_id = await db.scalar(
                select(AiAgent.agent_id).where(AiAgent.code == reference.agent_code)
            )
            if (
                agent_id is None
                or selection.provider_id != reference.resolved_provider_id
            ):
                raise _order_error()
            keys = (*selection.keys, ResourceKey(K.AGENT, None, agent_id))
        action, conversation, source, log = await lifecycle_candidate._locked(
            db,
            tx,
            reference,
            tenant,
            identity,
            SimpleNamespace(locks=LockPlan(existing=keys)),
        )
        if binding is not None:
            binding.require(action)
        if action.status in {"succeeded", "failed", "expired", "rejected"}:
            verify_read_terminal(action, log)
            return action
        expected = {
            "tenant_id": tenant.tenant_id,
            "user_id": tenant.actor_user_id,
            "conversation_id": action.conversation_id,
            "source_user_message_id": action.source_user_message_id,
            "trace_id": action.trace_id,
            "agent_code": action.agent_code,
            "tool_name": action.execute_tool_name,
            "tool_call_id": action.execute_tool_call_id,
            "confirmation_id": action.confirmation_id,
            "args_hash": action.args_hash,
            "readonly_snapshot": True,
            "status": "pending_confirmation",
            "execution_mode": "hitl",
            "started_at": None,
            "finished_at": None,
            "error_code": None,
            "approved_by": None,
            "result_summary": None,
        }
        if (
            action.status != "pending_confirmation"
            or action.row_version != 1
            or action.execution_owner is not None
            or action.execution_lease_expires_at is not None
            or action.finished_at is not None
            or action.approved_at is not None
            or action.error_code is not None
            or has_annotation(log.result_summary)
            or any(getattr(log, key) != value for key, value in expected.items())
        ):
            raise _order_error()
        if decision == "read":
            return action
        now = await tx.database_now()
        pa_key = ResourceKey(K.PREPARED_ACTION, tenant.tenant_id, action.action_id)
        log_key = ResourceKey(K.OPERATION_LOG, tenant.tenant_id, log.log_id)
        code = None
        if action.expires_at <= now:
            status, code = "expired", "AI_HITL_EXPIRED"
        elif decision == "timeout":
            raise _order_error()
        elif decision == "reject":
            status = "rejected"
        elif decision == "abort":
            status, code = "expired", "AI_INTERNAL_ERROR"
        elif disabled:
            status, code = "expired", "AI_USER_DISABLED"
        elif not source.is_active or conversation.deleted_at is not None:
            status, code = "expired", "AI_PREPARED_ACTION_SOURCE_STALE"
        else:
            status = "execute"
        if status != "execute":
            await tx.update_factual(
                pa_key,
                {
                    "status": status,
                    "row_version": 2,
                    "finished_at": now,
                    "error_code": code,
                    "duration_ms": 0,
                },
                expected={"status": "pending_confirmation", "row_version": 1},
            )
            await tx.update_factual(
                log_key,
                {
                    "status": status,
                    "finished_at": max(now.replace(tzinfo=None), log.queued_at),
                    "error_code": code,
                    "duration_ms": 0,
                },
                expected={"status": "pending_confirmation", "finished_at": None},
            )
            return await lifecycle_candidate.reference(
                db, confirmation_id, tenant=tenant
            )
        if selection is None:
            raise _order_error()
        await recheck_model(db, tx, tenant, selection)
        original = action.snapshot.get("_authorization") or {}
        actor = await load_live_user_authority(db, tenant=tenant)
        scope_hash = await result_projection_service.compute_data_scope_hash(db, actor)
        dependencies = result_projection_service.projection_dependency_message_ids(
            source
        )
        if (
            original.get("resolvedModelId") != str(selection.model_id)
            or original.get("resolvedProviderId") != str(selection.provider_id)
            or original.get("dataScopeHash") != scope_hash
            or dependencies is None
            or tuple(map(str, dependencies))
            != tuple(action.projection_dependency_message_ids or ())
        ):
            raise AuthorizationException(error_code="AI_RESULT_PROJECTION_FORBIDDEN")
        started = time.monotonic()
        result = await gateway_read_candidate._read(
            db,
            tx,
            preflight=False,
            _locked=True,
            tenant=tenant,
            actor_user_version=actor_user_version,
            conversation_id=action.conversation_id,
            source_id=action.source_user_message_id,
            trace_id=action.trace_id,
            agent_code=action.agent_code,
            scope_hash=scope_hash,
            tool=action.execute_tool_name,
            args=action.frozen_args,
        )
        duration = int((time.monotonic() - started) * 1000)
        lineage = None
        if result.ok:
            if result.projection is None:
                raise _order_error()
            lineage = result_projection_service.freeze_lineage(
                tenant=tenant,
                agent_code=action.agent_code,
                tool_codes=[action.execute_tool_name],
                subject_refs=result.projection.subject_refs,
                data_scope_hash=scope_hash if result.projection.scope_bound else None,
                projection_dependency_message_ids=dependencies,
            )
        data, ui = (
            (result.data, asdict(result.ui) if result.ui else None)
            if result.ok
            else (None, None)
        )
        if len(json.dumps([data, ui], allow_nan=False).encode()) > 1024 * 1024:
            raise _order_error()
        finished = await tx.database_now()
        await tx.update_factual(
            pa_key,
            {
                "status": "approved",
                "row_version": 2,
                "approved_at": now,
                "approved_by": tenant.actor_user_id,
            },
            expected={"status": "pending_confirmation", "row_version": 1},
        )
        await tx.update_factual(
            pa_key,
            {"status": "running", "row_version": 3},
            expected={"status": "approved", "row_version": 2},
        )
        changes = {
            "status": "succeeded" if result.ok else "failed",
            "row_version": 4,
            "finished_at": finished,
            "duration_ms": duration,
            "error_code": None
            if result.ok
            else result.error_code or "AI_INTERNAL_ERROR",
            "result_data": data,
            "result_ui": ui,
        }
        if lineage is not None:
            changes.update(
                tool_codes=list(lineage.tool_codes),
                subject_refs=list(lineage.subject_refs),
                subject_refs_hash=lineage.subject_refs_hash,
                data_scope_hash=lineage.data_scope_hash,
                resolver_version=lineage.resolver_version,
            )
        await tx.update_factual(
            pa_key, changes, expected={"status": "running", "row_version": 3}
        )
        action = await lifecycle_candidate.reference(db, confirmation_id, tenant=tenant)
        await tx.update_factual(
            log_key,
            {
                "status": "success" if result.ok else "failed",
                "started_at": max(now.replace(tzinfo=None), log.queued_at),
                "finished_at": max(finished.replace(tzinfo=None), log.queued_at),
                "approved_by": tenant.actor_user_id,
                "duration_ms": duration,
                "error_code": action.error_code,
                "result_summary": read_terminal_marker(action),
            },
            expected={"status": "pending_confirmation", "finished_at": None},
        )
        return action

    async def project(
        self, db, tx, confirmation_id, *, tenant, actor_user_version, binding
    ):
        action = await self.execute(
            db,
            tx,
            confirmation_id,
            tenant=tenant,
            actor_user_version=actor_user_version,
            decision="read",
            binding=binding,
        )
        if action.status not in {"succeeded", "failed", "expired", "rejected"}:
            return None
        actor = await load_live_user_authority(db, tenant=tenant)
        return await project_resume_terminal(db, action, actor, tenant)


read_confirmation_candidate = ReadConfirmationCandidate()
