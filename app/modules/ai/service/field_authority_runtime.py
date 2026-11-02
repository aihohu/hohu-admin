"""Transaction-bound AI evidence. Activation remains an all-entry release gate."""

from app.core.config import settings
from app.core.exceptions import AuthorizationException
from app.db.authorization_plan_session import _Journal
from app.modules.ai.service.field_authority import (
    denied,
    freeze_field_authority,
    require_current_field_authority,
)
from app.modules.system.field_policy.documents import catalog_digest
from app.modules.system.field_policy.user_fields import CATALOG_REVISION, USER_FIELDS
from app.modules.system.service.user_field_watermark import read_field_watermark


class AIFieldAuthority:
    async def visible(self, db, *, tenant, frozen):
        """For protected readers, provenance is an additional restriction."""
        journal = getattr(db, "_authorization_journal", None)
        tx = journal.handle if isinstance(journal, _Journal) else None
        if tx is None:
            # Legacy service consumers cannot authorize new evidence without a
            # barrier. Owners are mandatory for all production evidence readers.
            return frozen is None
        try:
            await self.require(db, tx, tenant=tenant, frozen=frozen, legacy=True)
        except AuthorizationException:
            return False
        return True

    async def current(self, db, tx, *, tenant):
        tx.assert_session(db)
        tx.assert_barrier(tenant.tenant_id)
        state, epoch = await read_field_watermark(db, tx, tenant_id=tenant.tenant_id)
        if (
            state is None
            or epoch is None
            or state.mode not in {"legacy", "ready"}
            or state.ever_enforced
            or state.active_manifest_digest is not None
            or state.catalog_revision != CATALOG_REVISION
            or state.catalog_digest != catalog_digest()
        ):
            raise denied("FIELD_POLICY_UNAVAILABLE")
        # Conservative dependency closure for opaque model text. Structured tool
        # adapters retain their narrower, code-owned output allowlists as a cap.
        return freeze_field_authority(
            tenant_id=tenant.tenant_id,
            actor_id=tenant.actor_user_id,
            global_epoch=epoch,
            tenant_epoch=state.epoch,
            mode=state.mode,
            active_digest=state.active_manifest_digest,
            build=settings.RELEASE_BUILD_SHA or "development",
            purpose="ai.run",
            fields=tuple(name for name, field in USER_FIELDS.items() if field.readable),
        )

    async def require(
        self, db, tx, *, tenant, frozen, confirmation=False, legacy=False
    ):
        current = await self.current(db, tx, tenant=tenant)
        if frozen is None and legacy:
            # Only the explicit never-enforced legacy gate above permits this.
            return None
        require_current_field_authority(frozen, current, confirmation=confirmation)
        return current


ai_field_authority = AIFieldAuthority()
