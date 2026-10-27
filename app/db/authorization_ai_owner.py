"""Dormant AI transaction owners. Recovery never receives a handler callback."""

import asyncio
from dataclasses import dataclass

from sqlalchemy import select, tuple_

from app.core.authorization_plan import TenantLock, TransactionBudget
from app.db.authorization_owner import (
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.authorization_recovery_candidate import (
    ai_recovery_candidate,
)
from app.modules.ai.service.completion_recovery_service import RecoveryOutcome
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


@dataclass(frozen=True, slots=True)
class _FactLocator:
    """DB-discovered identity for facts only; deliberately not a TenantContext."""

    tenant_id: int
    actor_user_id: int


@dataclass(frozen=True, slots=True)
class RecoveryItem:
    tenant_id: int
    action_id: int
    disposition: str


@dataclass(frozen=True, slots=True)
class RecoveryPage:
    items: tuple[RecoveryItem, ...]
    next_cursor: tuple[int, int] | None


class AIAuthorizationOwner:
    def __init__(self, factory):
        self.factory = factory

    async def recover_page(self, *, after=None, limit=256):
        """Discover only, release that snapshot, then protect each tenant afresh."""
        if (
            type(limit) is not int
            or not 1 <= limit <= 256
            or (
                after is not None
                and (
                    type(after) is not tuple
                    or len(after) != 2
                    or any(
                        type(value) is not int or not 0 <= value < 2**63
                        for value in after
                    )
                )
            )
        ):
            raise _order_error()
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with asyncio.timeout(5):
                        query = (
                            select(
                                AiPreparedAction.tenant_id,
                                AiPreparedAction.action_id,
                                AiPreparedAction.user_id,
                                AiPreparedAction.confirmation_id,
                            )
                            .where(AiPreparedAction.status == "running")
                            .order_by(
                                AiPreparedAction.tenant_id, AiPreparedAction.action_id
                            )
                            .limit(limit)
                        )
                        if after is not None:
                            query = query.where(
                                tuple_(
                                    AiPreparedAction.tenant_id,
                                    AiPreparedAction.action_id,
                                )
                                > after
                            )
                        rows = (await db.execute(query)).all()
            finally:
                await rollback_authorization_session(db)
        items = []
        deadline = asyncio.get_running_loop().time() + 30
        for row in rows:
            if items and asyncio.get_running_loop().time() >= deadline:
                break
            outcome = await self.reconcile(
                row.confirmation_id, tenant=_FactLocator(row.tenant_id, row.user_id)
            )
            items.append(
                RecoveryItem(row.tenant_id, row.action_id, outcome.disposition)
            )
        return RecoveryPage(
            tuple(items), (items[-1].tenant_id, items[-1].action_id) if items else None
        )

    async def reconcile(
        self, confirmation_id, *, tenant, expected_claim=None, allow_fence=False
    ):
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        result = await ai_recovery_candidate.reconcile(
                            db,
                            tx,
                            confirmation_id,
                            tenant=tenant,
                            expected_claim=expected_claim,
                            allow_fence=allow_fence,
                        )
            except BaseException as exc:
                await rollback_authorization_session(db)
                if not isinstance(exc, Exception):
                    raise
                return RecoveryOutcome(None)
            # rollback expires even expire_on_commit=False sessions. Preserve
            # the already-validated scalar snapshot before releasing this owner;
            # it carries facts only, never a reusable execution proof.
            if result.action is not None:
                db.expunge(result.action)
            if result.changed:
                # ACK uncertainty is never recast as a definitive rollback or
                # followed by another fencing attempt. The caller may read later.
                try:
                    await commit_authorization_session(db)
                except Exception:
                    return RecoveryOutcome(None)
            else:
                await rollback_authorization_session(db)
            return result
