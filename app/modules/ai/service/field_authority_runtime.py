"""Transaction-bound AI evidence. Activation remains an all-entry release gate."""

from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import _Journal
from app.modules.ai.service.delivery_authority import (
    freeze_delivery_authority,
    require_delivery_authority,
)
from app.modules.ai.service.delivery_source_domains import (
    authority_header,
    validate_receipt,
    with_sources,
)
from app.modules.ai.service.delivery_source_revalidation import revalidate_sources
from app.modules.ai.service.field_authority import (
    denied,
    freeze_field_authority,
    require_current_field_authority,
)
from app.modules.system.field_policy.documents import catalog_digest
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.field_policy.user_fields import CATALOG_REVISION, USER_FIELDS
from app.modules.system.models.field_policy import FieldPolicyHead
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service,
)
from app.modules.system.service.user_field_active import active_policy_grants
from app.modules.system.service.user_field_watermark import read_field_watermark


async def generic_stamp(db, tx, *, tenant):
    head = await db.scalar(
        select(FieldPolicyHead)
        .where(FieldPolicyHead.tenant_id == tenant.tenant_id)
        .execution_options(populate_existing=True)
    )
    if head is None:
        return None
    catalog = production_catalog()
    # This validates the complete active manifest. An empty role filter checks
    # integrity without inventing any field grant for the receipt itself.
    await generic_field_policy_service.load_active(
        db,
        tx,
        tenant_id=tenant.tenant_id,
        catalog=catalog,
        object_id="ai.message",
        role_ids=(),
    )
    epoch = await db.scalar(
        select(UserFieldPolicyGlobalGuard.epoch).where(
            UserFieldPolicyGlobalGuard.guard_id == 1
        )
    )
    tenant_epoch = await db.scalar(
        select(UserFieldPolicyState.epoch).where(
            UserFieldPolicyState.tenant_id == tenant.tenant_id
        )
    )
    return freeze_delivery_authority(
        tenant_id=tenant.tenant_id,
        actor_id=tenant.actor_user_id,
        global_epoch=epoch,
        tenant_epoch=tenant_epoch,
        active_digest=head.active_manifest_digest,
        build=settings.RELEASE_BUILD_SHA or "development",
    )


class AIFieldAuthority:
    async def visible(self, db, *, tenant, frozen, actor_user_version=None):
        """For protected readers, provenance is an additional restriction."""
        journal = getattr(db, "_authorization_journal", None)
        tx = journal.handle if isinstance(journal, _Journal) else None
        if tx is None:
            # Legacy service consumers cannot authorize new evidence without a
            # barrier. Owners are mandatory for all production evidence readers.
            return frozen is None
        try:
            if validate_receipt(frozen) is not None:
                if actor_user_version is None:
                    return False
                await self.renew(
                    db,
                    tx,
                    tenant=tenant,
                    actor_user_version=actor_user_version,
                    frozen=frozen,
                )
                return True
            await self.require(db, tx, tenant=tenant, frozen=frozen, legacy=True)
        except BusinessException:
            return False
        return True

    async def mint(self, db, tx, *, tenant, domains):
        return with_sources(await self.current(db, tx, tenant=tenant), tuple(domains))

    async def renew(self, db, tx, *, tenant, actor_user_version, frozen):
        if validate_receipt(frozen) is None:
            raise denied("FIELD_CONFIRMATION_LINEAGE_REQUIRED")
        current = await self.current(db, tx, tenant=tenant)
        header = authority_header(frozen)
        if any(
            header[name] != current[name] for name in ("tenant_id", "actor_id", "build")
        ):
            raise denied()
        await revalidate_sources(
            db, tx, tenant=tenant, actor_user_version=actor_user_version, receipt=frozen
        )
        return with_sources(current, tuple(frozen["source_domains"]))

    async def current(self, db, tx, *, tenant):
        tx.assert_session(db)
        tx.assert_barrier(tenant.tenant_id)
        generic = await generic_stamp(db, tx, tenant=tenant)
        if generic is not None:
            return generic
        await active_policy_grants(db, tx, tenant_id=tenant.tenant_id)
        state, epoch = await read_field_watermark(db, tx, tenant_id=tenant.tenant_id)
        if (
            state is None
            or epoch is None
            or state.mode not in {"legacy", "ready", "enforcing"}
            or (
                state.mode == "enforcing"
                and (not state.ever_enforced or state.active_manifest_digest is None)
            )
            or (
                state.mode in {"legacy", "ready"}
                and (state.ever_enforced or state.active_manifest_digest is not None)
            )
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
        if validate_receipt(frozen) is not None:
            frozen = authority_header(frozen)
        if current.get("schema") == 2:
            require_delivery_authority(frozen, current)
            return current
        if frozen is None and legacy and current["mode"] in {"legacy", "ready"}:
            # Only the explicit never-enforced legacy gate above permits this.
            return None
        require_current_field_authority(frozen, current, confirmation=confirmation)
        return current


ai_field_authority = AIFieldAuthority()
