"""System-owned field state reads for internal, barrier-bound consumers."""

from dataclasses import dataclass

from sqlalchemy import select

from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)


@dataclass(frozen=True, slots=True)
class FieldWatermark:
    mode: str
    ever_enforced: bool
    epoch: int
    active_manifest_digest: str | None
    catalog_revision: str
    catalog_digest: str


async def read_field_watermark(db, tx, *, tenant_id):
    tx.assert_session(db)
    tx.assert_barrier(tenant_id)
    state = await db.scalar(
        select(UserFieldPolicyState)
        .where(UserFieldPolicyState.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    epoch = await db.scalar(
        select(UserFieldPolicyGlobalGuard.epoch).where(
            UserFieldPolicyGlobalGuard.guard_id == 1
        )
    )
    return (
        FieldWatermark(
            state.mode,
            state.ever_enforced,
            state.epoch,
            state.active_manifest_digest,
            state.catalog_revision,
            state.catalog_digest,
        )
        if state is not None
        else None,
        epoch,
    )
