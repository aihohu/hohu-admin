"""Reconcile locked completion facts inside a caller-owned transaction.

Only DB protocol claims can be fenced after their previous transaction has
released its context locks. This service never commits, executes a tool, or
infers file/legacy outcomes from the absence of business rows.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import (
    DB_TOOLS,
    FILE_TOOLS,
    ClaimBinding,
    LockedClaim,
    atomic_completion_service,
    is_protocol_owner,
)


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """A locked observation; changed facts are not durable until caller commits."""

    action: AiPreparedAction | None
    changed: bool = False
    disposition: Literal["succeeded", "failed", "unknown"] = "unknown"


class CompletionRecoveryService:
    @staticmethod
    def _terminal_outcome(action: AiPreparedAction) -> RecoveryOutcome:
        if (
            action.status == "succeeded"
            and action.execute_tool_name in DB_TOOLS | FILE_TOOLS
        ) or (action.status == "failed" and action.execute_tool_name in DB_TOOLS):
            return RecoveryOutcome(action, disposition=action.status)
        return RecoveryOutcome(action)

    async def reconcile(
        self,
        db: AsyncSession,
        confirmation_id: str,
        *,
        tenant: TenantContext,
        expected_claim: ClaimBinding | None = None,
        allow_fence: bool = False,
    ) -> RecoveryOutcome:
        """Observe facts, or fence an eligible DB claim without taking it over.

        An original claim plus explicit ``allow_fence`` identifies a caller's
        uncertain commit. Ordinary reconciliation may only fence an expired
        lease, measured against the DB clock after acquiring all context locks.
        Lock/SQL failures and completion failures propagate: the caller must
        roll back the entire transaction, including any partial completion CAS.
        """
        if expected_claim is not None and (
            expected_claim.confirmation_id != confirmation_id
            or expected_claim.tenant_id != tenant.tenant_id
            or expected_claim.actor_user_id != tenant.actor_user_id
        ):
            return RecoveryOutcome(None)

        action = await db.scalar(
            select(AiPreparedAction)
            .where(
                AiPreparedAction.tenant_id == tenant.tenant_id,
                AiPreparedAction.user_id == tenant.actor_user_id,
                AiPreparedAction.confirmation_id == confirmation_id,
            )
            .execution_options(populate_existing=True)
        )
        if action is None:
            return RecoveryOutcome(None)
        if action.execute_tool_name not in DB_TOOLS | FILE_TOOLS:
            return RecoveryOutcome(action)
        # No positive file-side-effect disposition exists in stage B.
        if action.execute_tool_name in FILE_TOOLS and action.status != "succeeded":
            return RecoveryOutcome(action)

        # Only validation errors before any mutation can become unknown here.
        # Never include complete_failed in this catch: its second CAS can fail.
        try:
            if expected_claim is not None:
                locked = await atomic_completion_service.lock_claim(
                    db,
                    expected_claim,
                    tenant=tenant,
                    require_active=False,
                    require_running=False,
                )
            elif action.status in {"succeeded", "failed"}:
                terminal = await atomic_completion_service.lock_terminal(
                    db, confirmation_id, tenant=tenant
                )
                return self._terminal_outcome(terminal.action)
            else:
                if action.status != "running" or not is_protocol_owner(
                    action.execution_owner, action.execute_tool_name
                ):
                    return RecoveryOutcome(action)
                log_id = await db.scalar(
                    select(AiOperationLog.log_id).where(
                        AiOperationLog.tenant_id == tenant.tenant_id,
                        AiOperationLog.user_id == tenant.actor_user_id,
                        AiOperationLog.tool_call_id == action.execute_tool_call_id,
                    )
                )
                if log_id is None:
                    return RecoveryOutcome(action)
                claim = ClaimBinding.from_action(action, log_id)
                locked = await atomic_completion_service.lock_claim(
                    db,
                    claim,
                    tenant=tenant,
                    require_active=False,
                    require_running=False,
                )
        except BusinessRuleException:
            return RecoveryOutcome(action)

        action = locked.action
        if action.status in {"succeeded", "failed"}:
            return self._terminal_outcome(action)
        return await self._reconcile_running(
            db,
            locked,
            tenant=tenant,
            explicit_commit_unknown=expected_claim is not None and allow_fence,
        )

    async def _reconcile_running(
        self,
        db: AsyncSession,
        locked: LockedClaim,
        *,
        tenant: TenantContext,
        explicit_commit_unknown: bool,
    ) -> RecoveryOutcome:
        action = locked.action
        lease = action.execution_lease_expires_at
        if (
            action.execute_tool_name not in DB_TOOLS
            or not isinstance(lease, datetime)
            or lease.tzinfo is None
            or action.error_code is not None
        ):
            return RecoveryOutcome(action)
        now = await db.scalar(select(func.clock_timestamp()))
        if not explicit_commit_unknown and lease > now:
            return RecoveryOutcome(action)
        terminal = await atomic_completion_service.complete_failed(
            db,
            locked,
            error_code="AI_PREPARED_ACTION_EXECUTION_INTERRUPTED",
            duration_ms=0,
            tenant=tenant,
        )
        return RecoveryOutcome(terminal, changed=True, disposition="failed")


completion_recovery_service = CompletionRecoveryService()
