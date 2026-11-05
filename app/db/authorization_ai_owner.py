"""Dormant AI transaction owners. Recovery never receives a handler callback."""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, tuple_

from app.core.authorization_plan import LockPlan, TenantLock, TransactionBudget
from app.core.tenant import TenantContext
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
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)

logger = logging.getLogger(__name__)


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
    changed: bool = False


@dataclass(frozen=True, slots=True)
class RecoveryPage:
    items: tuple[RecoveryItem, ...]
    next_cursor: tuple[int, int] | None


class AIAuthorizationOwner:
    def __init__(self, factory):
        self.factory = factory

    async def cutoff(self):
        async with self.factory() as db:
            try:
                async with authorization_transactions.scope(
                    db, tenants=(), budget=TransactionBudget(timeout_ms=5000)
                ) as tx:
                    await tx.lock(LockPlan())
                    return await tx.database_now()
            finally:
                await rollback_authorization_session(db)

    async def orphan_cached(self, *, pending, confirmation_id, redis, startup_before):
        from app.modules.ai.agents.hitl.manager import (  # noqa: PLC0415
            PendingPayload,
            hitl_manager,
        )
        from app.modules.ai.service.authorization_orphan_candidate import (  # noqa: PLC0415
            orphan_candidate,
        )
        from app.modules.ai.service.chat_run_service import (  # noqa: PLC0415
            chat_run_guard,
        )

        if (
            type(pending) is not PendingPayload
            or pending.action_id is not None
            or any(
                type(n) is not int or not 0 <= n < 2**63
                for n in (
                    pending.tenant_id,
                    pending.user_id,
                    pending.conversation_id,
                    pending.source_user_message_id,
                )
            )
            or not isinstance(startup_before, datetime)
            or startup_before.tzinfo is None
            or type(confirmation_id) is not str
            or not 1 <= len(confirmation_id) <= 64
            or len(json.dumps(pending.__dict__, allow_nan=False).encode()) > 65536
        ):
            return False
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(pending.tenant_id),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        row = await db.get(Tenant, pending.tenant_id)
                        if row is None:
                            return False
                        tenant = TenantContext(
                            row.tenant_id,
                            row.tenant_code,
                            pending.user_id,
                            row.row_version,
                            "platform_control",
                        )
                        outcome = await orphan_candidate.settle(
                            db,
                            tx,
                            tenant=tenant,
                            pending=pending,
                            confirmation_id=confirmation_id,
                            startup_before=startup_before,
                        )
                if outcome is not None and outcome.changed:
                    await commit_authorization_session(db)
                else:
                    await rollback_authorization_session(db)
            except BaseException as exc:
                await rollback_authorization_session(db)
                if not isinstance(exc, Exception):
                    raise
                return False
        if outcome is None:
            return False
        try:
            async with asyncio.timeout(2):
                if pending.guard_owner_token:
                    await chat_run_guard.release(
                        redis,
                        conversation_id=pending.conversation_id,
                        owner_token=pending.guard_owner_token,
                        tenant=tenant,
                    )
                await hitl_manager.delete_pending(redis, confirmation_id, tenant=tenant)
        except Exception:
            logger.warning("Confirmed orphan recovery retained its cache hint")
        if outcome.status != "success":
            from app.db.authorization_chat_owner import ChatOwner  # noqa: PLC0415

            try:
                await ChatOwner(self.factory).recover_orphan(
                    tenant=tenant, confirmation_id=confirmation_id
                )
            except Exception:
                logger.warning("Confirmed orphan recovery display unavailable")
        return True

    async def sweep_page(
        self,
        *,
        after=None,
        limit=256,
        startup=False,
        redis=None,
        startup_before=None,
        repair_display=False,
    ):
        if (
            type(startup) is not bool
            or type(repair_display) is not bool
            or (
                startup_before is not None
                and (
                    not isinstance(startup_before, datetime)
                    or startup_before.tzinfo is None
                )
            )
            or type(limit) is not int
            or not 1 <= limit <= 256
            or (
                after is not None
                and (
                    type(after) is not tuple
                    or len(after) != 2
                    or any(type(n) is not int or not 0 <= n < 2**63 for n in after)
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
                            .where(
                                AiPreparedAction.status.in_(
                                    ("pending_confirmation", "approved", "running")
                                )
                            )
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
                        if startup_before is not None:
                            query = query.where(
                                AiPreparedAction.created_at <= startup_before
                            )
                        rows = (await db.execute(query)).all()
            finally:
                await rollback_authorization_session(db)
        items = []
        deadline = asyncio.get_running_loop().time() + 30
        for row in rows:
            if items and asyncio.get_running_loop().time() >= deadline:
                break
            items.append(
                await self._sweep_one(
                    row,
                    startup=startup,
                    startup_before=startup_before,
                    redis=redis,
                    repair_display=repair_display,
                )
            )
        return RecoveryPage(
            tuple(items),
            (items[-1].tenant_id, items[-1].action_id)
            if items and (len(items) != len(rows) or len(rows) == limit)
            else None,
        )

    async def observe_cached(self, *, tenant_id, user_id, confirmation_id, redis):
        if (
            any(type(n) is not int or not 0 <= n < 2**63 for n in (tenant_id, user_id))
            or not isinstance(confirmation_id, str)
            or not 1 <= len(confirmation_id) <= 128
        ):
            raise _order_error()
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with asyncio.timeout(5):
                        row = (
                            await db.execute(
                                select(
                                    AiPreparedAction.tenant_id,
                                    AiPreparedAction.action_id,
                                    AiPreparedAction.user_id,
                                    AiPreparedAction.confirmation_id,
                                ).where(
                                    AiPreparedAction.tenant_id == tenant_id,
                                    AiPreparedAction.user_id == user_id,
                                    AiPreparedAction.confirmation_id == confirmation_id,
                                )
                            )
                        ).one_or_none()
            finally:
                await rollback_authorization_session(db)
        if row is None:
            return None
        return await self._sweep_one(
            row, startup=False, startup_before=None, redis=redis, repair_display=True
        )

    async def _sweep_one(self, row, *, startup, startup_before, redis, repair_display):
        from app.modules.ai.service.authorization_lifecycle_candidate import (  # noqa: PLC0415
            lifecycle_candidate,
        )

        status, action, context, changed = "unknown", None, None, False
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(row.tenant_id),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        outcome = await lifecycle_candidate.sweep(
                            db,
                            tx,
                            row.confirmation_id,
                            locator=_FactLocator(row.tenant_id, row.user_id),
                            startup=startup,
                            startup_before=startup_before,
                        )
                        if outcome.action is not None:
                            tenant_row = await db.get(Tenant, row.tenant_id)
                            context = TenantContext(
                                row.tenant_id,
                                tenant_row.tenant_code,
                                row.user_id,
                                tenant_row.row_version,
                                "platform_control",
                            )
                        db.expunge_all()
                if outcome.changed:
                    await commit_authorization_session(db)
                else:
                    await rollback_authorization_session(db)
                status, action = outcome.disposition, outcome.action
                changed = outcome.changed
            except BaseException as exc:
                await rollback_authorization_session(db)
                if not isinstance(exc, Exception):
                    raise
        if redis is not None and action is not None:
            await self._sweep_cache(redis, action, context, status, startup=startup)
        if (
            repair_display
            and action is not None
            and status in {"expired", "failed", "succeeded"}
        ):
            from app.db.authorization_chat_owner import ChatOwner  # noqa: PLC0415

            try:
                await ChatOwner(self.factory).recover(
                    tenant=context, confirmation_id=action.confirmation_id
                )
            except Exception:
                logger.warning("Confirmed recovery display unavailable")
        return RecoveryItem(row.tenant_id, row.action_id, status, changed)

    async def _sweep_cache(self, redis, action, tenant, status, *, startup):
        from app.modules.ai.agents.hitl.manager import hitl_manager  # noqa: PLC0415
        from app.modules.ai.service.chat_run_service import (  # noqa: PLC0415
            chat_run_guard,
        )

        try:
            async with asyncio.timeout(2):
                if status == "pending_confirmation":
                    if startup and action.guard_owner_token:
                        acquired = await chat_run_guard.acquire(
                            redis,
                            conversation_id=action.conversation_id,
                            owner_token=action.guard_owner_token,
                            tenant=tenant,
                        )
                        if acquired:
                            await chat_run_guard.handoff_pending(
                                redis,
                                conversation_id=action.conversation_id,
                                owner_token=action.guard_owner_token,
                                tenant=tenant,
                                confirmation_ttl_sec=max(
                                    1,
                                    int(
                                        (
                                            action.expires_at - datetime.now(UTC)
                                        ).total_seconds()
                                    ),
                                ),
                            )
                elif status in {"expired", "failed", "succeeded", "rejected"}:
                    if action.guard_owner_token:
                        await chat_run_guard.release(
                            redis,
                            conversation_id=action.conversation_id,
                            owner_token=action.guard_owner_token,
                            tenant=tenant,
                        )
                    await hitl_manager.delete_pending(
                        redis, action.confirmation_id, tenant=tenant
                    )
        except Exception:
            logger.warning("Confirmed recovery retained its cache hint")

    async def expire_page(self, *, after=None, limit=256, startup=False):
        # Delayed import keeps factual recovery independent from execution.
        from app.modules.ai.service.authorization_lifecycle_candidate import (  # noqa: PLC0415
            lifecycle_candidate,
        )

        if (
            type(startup) is not bool
            or type(limit) is not int
            or not 1 <= limit <= 256
            or (
                after is not None
                and (
                    type(after) is not tuple
                    or len(after) != 2
                    or any(type(n) is not int or not 0 <= n < 2**63 for n in after)
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
                            .where(AiPreparedAction.status == "pending_confirmation")
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
            status = "unknown"
            async with self.factory() as db:
                try:
                    with bounded_authorization_reads(db):
                        async with authorization_transactions.scope(
                            db,
                            tenants=(TenantLock(row.tenant_id),),
                            budget=TransactionBudget(timeout_ms=8000),
                        ) as tx:
                            result = await lifecycle_candidate.expire(
                                db,
                                tx,
                                row.confirmation_id,
                                locator=_FactLocator(row.tenant_id, row.user_id),
                                startup=startup,
                            )
                except BaseException as exc:
                    await rollback_authorization_session(db)
                    if not isinstance(exc, Exception):
                        raise
                else:
                    try:
                        await commit_authorization_session(db)
                    except Exception:
                        pass
                    else:
                        status = result
            items.append(RecoveryItem(row.tenant_id, row.action_id, status))
        return RecoveryPage(
            tuple(items), (items[-1].tenant_id, items[-1].action_id) if items else None
        )

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
