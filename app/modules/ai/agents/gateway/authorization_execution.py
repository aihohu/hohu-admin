"""Opt-in Gateway transaction owner; never invokes an arbitrary tool handler."""

import asyncio
import logging
from copy import deepcopy

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.tenant import TenantContext
from app.db.authorization_ai_owner import AIAuthorizationOwner
from app.db.authorization_owner import (
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.modules.ai.service.atomic_completion_service import ClaimBinding
from app.modules.ai.service.authorization_execution_candidate import (
    ExecutionDisposition,
    ai_execution_candidate,
)
from app.modules.system.service.authorization_ai_tool_adapter import (
    system_ai_tool_adapter,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)

logger = logging.getLogger(__name__)


class PlannedGatewayOwner:
    def __init__(self, factory):
        self.factory = factory

    async def _facts(self, claim, tenant, *, fence):
        result = await AIAuthorizationOwner(self.factory).reconcile(
            claim.confirmation_id,
            tenant=tenant,
            expected_claim=claim,
            allow_fence=fence,
        )
        return ExecutionDisposition(result.disposition)

    async def execute_claim(self, claim, *, tenant, actor_user_version):
        if (
            type(claim) is not ClaimBinding
            or type(tenant) is not TenantContext
            or type(actor_user_version) is not int
            or actor_user_version < 1
        ):
            raise _order_error()
        terminal = False
        failed = False
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with asyncio.timeout(5):
                        action = await ai_execution_candidate.reference(
                            db, claim, tenant=tenant
                        )
                        terminal = action.status != "running"
                        tool, args, snapshot = (
                            action.execute_tool_name,
                            deepcopy(action.frozen_args),
                            deepcopy(action.snapshot),
                        )
                await rollback_authorization_session(db)
                if not terminal:
                    command = await system_ai_tool_adapter.prepare_input(
                        db,
                        tool=tool,
                        args=args,
                        snapshot=snapshot.get("business"),
                        tenant=tenant,
                        actor_user_version=actor_user_version,
                    )
                    with bounded_authorization_reads(db):
                        async with authorization_transactions.scope(
                            db,
                            tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),),
                            budget=TransactionBudget(timeout_ms=60000),
                        ) as tx:
                            outcome = await ai_execution_candidate.execute(
                                db,
                                tx,
                                command,
                                claim,
                                tenant=tenant,
                                actor_user_version=actor_user_version,
                            )
            except BaseException as exc:
                try:
                    await rollback_authorization_session(db)
                except Exception:
                    return ExecutionDisposition("unknown")
                if not isinstance(exc, Exception):
                    raise
                logger.warning(
                    "Planned execution rolled back: %s (%s)",
                    type(exc).__name__,
                    getattr(exc, "error_code", None),
                )
                failed = True
            if not terminal and not failed:
                try:
                    await commit_authorization_session(db)
                except Exception:
                    # No fencing, callback, retry or business replay after unknown ACK.
                    return ExecutionDisposition("unknown")
                return outcome
        # A fresh fact-only transaction follows only a positively acknowledged
        # rollback. Duplicate terminal reads never carry persisted result bodies.
        return await self._facts(claim, tenant, fence=failed)
