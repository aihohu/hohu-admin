"""Shared deadline and authority facts for detached transfer delivery."""

import asyncio
from dataclasses import dataclass, field

from sqlalchemy import select, true

from app.core.authorization_plan import TransactionBudget
from app.db.authorization_plan_session import _order_error, _timeout_error
from app.modules.system.models.field_policy import FieldPolicyHead as Head
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard as Guard,
)
from app.modules.system.models.user_field_policy import UserFieldPolicyState as Barrier
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    SourceReadBudget,
    authorization_read_phase,
)
from app.modules.system.service.generic_field_policy_service import failure


def remaining_budget(deadline, maximum_ms):
    remaining = int((deadline - asyncio.get_running_loop().time()) * 1000)
    if remaining < 1:
        raise _timeout_error()
    return TransactionBudget(timeout_ms=min(remaining, maximum_ms))


@dataclass
class DeliveryReadBudget(AuthorizationReadBudget):
    source_budget: SourceReadBudget = field(default_factory=SourceReadBudget)


async def capture_delivery_watermark(db, tx, *, tenant, actor):
    tx.assert_session(db)
    tx.assert_barrier(tenant.tenant_id)
    if actor.user_id != tenant.actor_user_id or type(actor.auth_version) is not int:
        raise _order_error()
    with authorization_read_phase(db, "barrier"):
        row = (
            await db.execute(
                select(
                    Guard.epoch,
                    Barrier.epoch,
                    Barrier.mode,
                    Barrier.ever_enforced,
                    Barrier.catalog_revision,
                    Barrier.catalog_digest,
                    Barrier.active_manifest_digest,
                    Barrier.candidate_manifest_digest,
                    Head.catalog_digest,
                    Head.head_version,
                    Head.rollout_state,
                    Head.ever_enforced,
                    Head.active_manifest_id,
                    Head.active_manifest_digest,
                    Head.candidate_manifest_id,
                    Head.candidate_manifest_digest,
                    Tenant.row_version,
                    Tenant.status,
                    Tenant.lifecycle_state,
                    Tenant.bootstrap_version,
                )
                .select_from(Barrier)
                .join(Guard, true())
                .join(Tenant, Tenant.tenant_id == Barrier.tenant_id)
                .outerjoin(Head, Head.tenant_id == Barrier.tenant_id)
                .where(Barrier.tenant_id == tenant.tenant_id, Guard.guard_id == 1)
                .limit(1)
            )
        ).one_or_none()
    if row is None:
        raise failure()
    return (tenant.tenant_id, actor.user_id, actor.auth_version, *tuple(row))
