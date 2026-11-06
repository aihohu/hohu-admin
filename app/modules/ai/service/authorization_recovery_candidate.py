"""Proof-only recovery; this service has no execution or current-authority path."""

from datetime import datetime

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import (
    DB_TOOLS,
    FILE_TOOLS,
    ClaimBinding,
    _check_action,
    _check_log,
    atomic_completion_service,
    is_protocol_owner,
)
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.completion_recovery_service import RecoveryOutcome


class AIRecoveryCandidate:
    async def reconcile(
        self, db, tx, confirmation_id, *, tenant, expected_claim=None, allow_fence=False
    ):
        tx.assert_session(db)
        if expected_claim is not None and (
            type(expected_claim) is not ClaimBinding
            or expected_claim.confirmation_id != confirmation_id
            or expected_claim.tenant_id != tenant.tenant_id
            or expected_claim.actor_user_id != tenant.actor_user_id
        ):
            await tx.lock(LockPlan())
            return RecoveryOutcome(None)
        reference = (
            await db.execute(
                select(
                    AiPreparedAction.action_id, AiPreparedAction.conversation_id
                ).where(
                    AiPreparedAction.tenant_id == tenant.tenant_id,
                    AiPreparedAction.user_id == tenant.actor_user_id,
                    AiPreparedAction.confirmation_id == confirmation_id,
                )
            )
        ).one_or_none()
        if reference is None:
            await tx.lock(LockPlan())
            return RecoveryOutcome(None)
        inventory = await ai_context_candidate.discover(
            db,
            tx,
            tenant_id=tenant.tenant_id,
            conversation_ids=(reference.conversation_id,),
        )
        await tx.lock(LockPlan(existing=inventory.keys))
        await ai_context_candidate.recheck(db, tx, inventory)
        action = await db.scalar(
            select(AiPreparedAction)
            .where(
                AiPreparedAction.tenant_id == tenant.tenant_id,
                AiPreparedAction.action_id == reference.action_id,
            )
            .execution_options(populate_existing=True)
        )
        if (
            action is None
            or action.user_id != tenant.actor_user_id
            or action.confirmation_id != confirmation_id
        ):
            return RecoveryOutcome(None)
        conversation = await db.scalar(
            select(AiConversation).where(
                AiConversation.tenant_id == tenant.tenant_id,
                AiConversation.conversation_id == action.conversation_id,
            )
        )
        source = await db.scalar(
            select(AiMessage).where(
                AiMessage.tenant_id == tenant.tenant_id,
                AiMessage.message_id == action.source_user_message_id,
            )
        )
        log = await db.scalar(
            select(AiOperationLog).where(
                AiOperationLog.tenant_id == tenant.tenant_id,
                AiOperationLog.tool_call_id == action.execute_tool_call_id,
            )
        )
        if (
            conversation is None
            or source is None
            or log is None
            or conversation.user_id != tenant.actor_user_id
            or source.conversation_id != action.conversation_id
            or source.role != "user"
            or source.trace_id != action.trace_id
        ):
            return RecoveryOutcome(None)
        tx.assert_held(
            ResourceKey(K.PREPARED_ACTION, tenant.tenant_id, action.action_id),
            ResourceKey(K.OPERATION_LOG, tenant.tenant_id, log.log_id),
        )
        if expected_claim is not None:
            _check_action(action, expected_claim, tenant, require_running=False)
            if log.log_id != expected_claim.log_id:
                return RecoveryOutcome(None)
        if action.status in {"succeeded", "failed"}:
            atomic_completion_service.validate_locked_terminal(
                action, log, tenant=tenant, conversation=conversation, source=source
            )
            if (
                action.status == "succeeded"
                and action.execute_tool_name in DB_TOOLS | FILE_TOOLS
            ) or (action.status == "failed" and action.execute_tool_name in DB_TOOLS):
                return RecoveryOutcome(action, disposition=action.status)
            return RecoveryOutcome(None)
        if (
            action.status != "running"
            or action.execute_tool_name not in DB_TOOLS
            or not is_protocol_owner(action.execution_owner, action.execute_tool_name)
        ):
            return RecoveryOutcome(None)
        claim = expected_claim or ClaimBinding.from_action(action, log.log_id)
        _check_action(action, claim, tenant, require_running=True)
        _check_log(action, log, "running")
        lease = action.execution_lease_expires_at
        now = await tx.database_now()
        if (
            not isinstance(lease, datetime)
            or lease.tzinfo is None
            or action.error_code is not None
            or (
                lease > now and not (expected_claim is not None and allow_fence is True)
            )
        ):
            return RecoveryOutcome(None)
        code = "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
        await tx.update_factual(
            ResourceKey(K.PREPARED_ACTION, tenant.tenant_id, action.action_id),
            {
                "status": "failed",
                "row_version": action.row_version + 1,
                "execution_lease_expires_at": None,
                "finished_at": now,
                "error_code": code,
                "duration_ms": 0,
            },
            expected={
                "status": "running",
                "row_version": claim.row_version,
                "execution_owner": claim.execution_owner,
            },
        )
        await tx.update_factual(
            ResourceKey(K.OPERATION_LOG, tenant.tenant_id, log.log_id),
            {
                "status": "failed",
                "finished_at": max(
                    now.replace(tzinfo=None), log.queued_at, log.started_at
                ),
                "error_code": code,
                "duration_ms": 0,
                "result_summary": "prepared action rolled back",
            },
            expected={"status": "running", "finished_at": None},
        )
        action = await db.scalar(
            select(AiPreparedAction)
            .where(
                AiPreparedAction.tenant_id == tenant.tenant_id,
                AiPreparedAction.action_id == action.action_id,
            )
            .execution_options(populate_existing=True)
        )
        return RecoveryOutcome(action, changed=True, disposition="failed")


ai_recovery_candidate = AIRecoveryCandidate()
