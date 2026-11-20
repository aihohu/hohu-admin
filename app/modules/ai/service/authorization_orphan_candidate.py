"""Factual cleanup for bound, registered readonly legacy HITL envelopes."""

from dataclasses import dataclass

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, ResourceKey, ResourceKind
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import DB_TOOLS, FILE_TOOLS
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.preflight_completion_service import has_preflight_marker
from app.modules.ai.service.reconciliation_annotation import has_annotation


@dataclass(frozen=True, slots=True)
class OrphanResult:
    status: str
    changed: bool = False


class OrphanCandidate:
    async def settle(self, db, tx, *, tenant, pending, confirmation_id, startup_before):
        tx.assert_session(db)
        inventory = await ai_context_candidate.discover(
            db,
            tx,
            tenant_id=tenant.tenant_id,
            conversation_ids=(pending.conversation_id,),
        )
        await tx.lock(LockPlan(existing=inventory.keys))
        await ai_context_candidate.recheck(db, tx, inventory)
        if (
            await db.scalar(
                select(AiPreparedAction.action_id).where(
                    AiPreparedAction.tenant_id == tenant.tenant_id,
                    AiPreparedAction.confirmation_id == confirmation_id,
                )
            )
            is not None
        ):
            return None
        registered = ToolRegistry.get().find(pending.tool_name)
        if (
            pending.tool_name in DB_TOOLS | FILE_TOOLS
            or registered is None
            or registered.meta.readonly is not True
        ):
            return None
        conversation = await db.get(
            AiConversation, pending.conversation_id, populate_existing=True
        )
        source = await db.get(
            AiMessage, pending.source_user_message_id, populate_existing=True
        )
        log = await db.scalar(
            select(AiOperationLog)
            .where(
                AiOperationLog.tenant_id == tenant.tenant_id,
                AiOperationLog.tool_call_id == pending.tool_call_id,
            )
            .execution_options(populate_existing=True)
        )
        if (
            conversation is None
            or conversation.tenant_id != tenant.tenant_id
            or conversation.user_id != pending.user_id
            or source is None
            or source.tenant_id != tenant.tenant_id
            or source.conversation_id != pending.conversation_id
            or source.role != "user"
            or source.trace_id != pending.trace_id
            or log is None
        ):
            return None
        expected = {
            "tenant_id": tenant.tenant_id,
            "user_id": pending.user_id,
            "conversation_id": pending.conversation_id,
            "source_user_message_id": pending.source_user_message_id,
            "trace_id": pending.trace_id,
            "tool_name": pending.tool_name,
            "agent_code": registered.meta.agent,
            "confirmation_id": confirmation_id,
            "args_hash": compute_args_hash(pending.args),
            "readonly_snapshot": True,
            "execution_mode": "hitl",
        }
        if pending.agent_code != registered.meta.agent or any(
            getattr(log, key) != value for key, value in expected.items()
        ):
            return None
        if has_annotation(log.result_summary) or has_preflight_marker(
            log.result_summary
        ):
            return None
        cutoff = startup_before.replace(tzinfo=None)
        if log.queued_at > cutoff or (
            log.started_at is not None and log.started_at > cutoff
        ):
            return None
        if log.status in {"success", "failed", "rejected", "expired"}:
            return OrphanResult(log.status) if log.finished_at is not None else None
        if log.finished_at is not None or log.error_code is not None:
            return None
        if log.status == "pending_confirmation" and log.started_at is None:
            status, code = "expired", "AI_HITL_EXPIRED"
        elif log.status == "running" and log.started_at is not None:
            status, code = "failed", "AI_HITL_EXECUTION_INTERRUPTED"
        else:
            return None
        now = (await tx.database_now()).replace(tzinfo=None)
        await tx.update_factual(
            ResourceKey(ResourceKind.OPERATION_LOG, tenant.tenant_id, log.log_id),
            {
                "status": status,
                "error_code": code,
                "finished_at": max(now, log.queued_at, log.started_at or log.queued_at),
                "duration_ms": log.duration_ms or 0,
            },
            expected={"status": log.status, "finished_at": None},
        )
        return OrphanResult(status, True)


orphan_candidate = OrphanCandidate()
