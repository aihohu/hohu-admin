"""Fixed audit admission; a recorded start never authorizes tool execution."""

import re
from types import SimpleNamespace

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.id_generator import next_id
from app.db.authorization_plan_session import _order_error
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.authorization_chat_candidate import chat_candidate
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.preflight_completion_service import (
    runtime_preflight_summary,
)
from app.modules.ai.service.reconciliation_annotation import has_annotation


class GatewayAuditCandidate:
    async def finish_operation(
        self,
        db,
        tx,
        *,
        tenant,
        log_id,
        succeeded,
        duration_ms,
        result_summary=None,
        target_summary=None,
        error_code=None,
    ):
        if (
            type(log_id) is not int
            or log_id <= 0
            or type(succeeded) is not bool
            or type(duration_ms) is not int
            or not 0 <= duration_ms <= 2**31 - 1
            or any(
                value is not None
                and (type(value) is not str or len(value.encode()) > 16384)
                for value in (result_summary, target_summary)
            )
            or (
                not succeeded
                and (type(error_code) is not str or not 1 <= len(error_code) <= 64)
            )
        ):
            raise _order_error()
        log = await db.scalar(
            select(AiOperationLog).where(
                AiOperationLog.tenant_id == tenant.tenant_id,
                AiOperationLog.log_id == log_id,
            )
        )
        if log is None:
            return False
        conversation_id = log.conversation_id
        inventory = await ai_context_candidate.discover(
            db, tx, tenant_id=tenant.tenant_id, conversation_ids=(conversation_id,)
        )
        await tx.lock(LockPlan(existing=inventory.keys))
        await ai_context_candidate.recheck(db, tx, inventory)
        log = await db.get(AiOperationLog, log_id, populate_existing=True)
        if (
            log is None
            or log.tenant_id != tenant.tenant_id
            or log.user_id != tenant.actor_user_id
        ):
            raise _order_error()
        action = await db.scalar(
            select(AiPreparedAction.action_id).where(
                AiPreparedAction.tenant_id == tenant.tenant_id,
                AiPreparedAction.execute_tool_call_id == log.tool_call_id,
            )
        )
        if (
            action is not None
            or log.status not in {"running", "pending_confirmation"}
            or has_annotation(log.result_summary)
        ):
            return False
        conversation = await db.get(
            AiConversation, conversation_id, populate_existing=True
        )
        source = await db.get(
            AiMessage, log.source_user_message_id, populate_existing=True
        )
        if (
            conversation is None
            or conversation.tenant_id != tenant.tenant_id
            or conversation.user_id != tenant.actor_user_id
            or source is None
            or source.tenant_id != tenant.tenant_id
            or source.conversation_id != conversation_id
            or source.trace_id != log.trace_id
            or source.role != "user"
            or (succeeded and (not log.readonly_snapshot or log.status != "running"))
        ):
            raise _order_error()
        now = (await tx.database_now()).replace(tzinfo=None)
        values = {
            "status": "success" if succeeded else "failed",
            "duration_ms": duration_ms,
            "finished_at": max(now, log.queued_at, log.started_at or log.queued_at),
        }
        if succeeded:
            values["result_summary"] = result_summary
            if target_summary is not None:
                values["target_summary"] = target_summary
        else:
            values["error_code"] = error_code
        await tx.update_factual(
            ResourceKey(K.OPERATION_LOG, tenant.tenant_id, log_id),
            values,
            expected={"status": log.status, "finished_at": None},
        )
        return True

    async def start_operation(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        conversation_id,
        source_id,
        trace_id,
        tool_name,
        agent_code,
        tool_call_id,
        args_hash,
        args_summary,
        readonly,
        risk,
        mode,
        injection_hit,
        preflight_error=None,
    ):
        if (
            any(type(v) is not int or v <= 0 for v in (conversation_id, source_id))
            or any(
                type(value) is not str or not 1 <= len(value) <= limit
                for value, limit in (
                    (trace_id, 64),
                    (tool_name, 128),
                    (agent_code, 64),
                    (tool_call_id, 64),
                )
            )
            or type(args_hash) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", args_hash)
            or type(args_summary) is not str
            or len(args_summary.encode()) > 16384
            or type(readonly) is not bool
            or type(injection_hit) is not bool
            or risk not in {"low", "high", "destructive"}
            or mode not in {"autonomous", "hitl"}
            or (
                preflight_error is not None
                and (
                    type(preflight_error) is not str
                    or not 1 <= len(preflight_error) <= 64
                )
            )
        ):
            raise _order_error()
        key = ResourceKey(K.OPERATION_LOG, tenant.tenant_id, next_id())
        parent = ResourceKey(K.CONVERSATION, tenant.tenant_id, conversation_id)
        await chat_candidate._lock(
            db,
            tx,
            tenant=tenant,
            actor_user_version=actor_user_version,
            conversation_id=conversation_id,
            new=(NewResource(key, parent),),
        )
        source = await db.get(AiMessage, source_id, populate_existing=True)
        if (
            source is None
            or source.tenant_id != tenant.tenant_id
            or source.conversation_id != conversation_id
            or source.role != "user"
            or not source.is_active
            or source.trace_id != trace_id
        ):
            raise _order_error()
        now = (await tx.database_now()).replace(tzinfo=None)
        running = mode == "autonomous" and preflight_error is None
        values = {
            "user_id": tenant.actor_user_id,
            "conversation_id": conversation_id,
            "source_user_message_id": source_id,
            "trace_id": trace_id,
            "tool_name": tool_name,
            "agent_code": agent_code,
            "tool_call_id": tool_call_id,
            "args_hash": args_hash,
            "args_summary": args_summary,
            "readonly_snapshot": readonly,
            "risk_level": risk,
            "execution_mode": mode,
            "status": "failed"
            if preflight_error is not None
            else "running"
            if running
            else "pending_confirmation",
            "queued_at": now,
            "started_at": now if running else None,
            "finished_at": now if preflight_error is not None else None,
            "duration_ms": 0 if preflight_error is not None else None,
            "hitl_wait_ms": 0 if running else None,
            "error_code": preflight_error,
            "confirmation_id": None,
            "approved_by": None,
            "target_summary": None,
            "is_security_event": injection_hit,
            "event_type": "injection_pattern_matched" if injection_hit else None,
        }
        if preflight_error is not None and not readonly:
            values["result_summary"] = runtime_preflight_summary(
                SimpleNamespace(
                    log_id=key.identity[0], tenant_id=tenant.tenant_id, **values
                )
            )
        await tx.insert_reserved(key, values)
        return key.identity[0]


gateway_audit_candidate = GatewayAuditCandidate()
