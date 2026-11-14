"""Explicit provenance for newly authored test inputs, never legacy backfill."""

from sqlalchemy import select

from app.core.config import settings
from app.modules.ai.service.delivery_authority import freeze_delivery_authority
from app.modules.ai.service.delivery_source_domains import source_domain, with_sources
from app.modules.ai.service.field_authority import freeze_field_authority
from app.modules.system.field_policy.user_fields import USER_FIELDS
from app.modules.system.models.field_policy import FieldPolicyHead
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)


async def authored_source(db, *, tenant, message_id):
    state = await db.get(UserFieldPolicyState, tenant.tenant_id)
    epoch = await db.scalar(
        select(UserFieldPolicyGlobalGuard.epoch).where(
            UserFieldPolicyGlobalGuard.guard_id == 1
        )
    )
    assert state is not None and epoch is not None, (
        "Fixture requires initialized authorization state"
    )
    head = await db.get(FieldPolicyHead, tenant.tenant_id)
    values = {
        "tenant_id": tenant.tenant_id,
        "actor_id": tenant.actor_user_id,
        "global_epoch": epoch,
        "tenant_epoch": state.epoch,
        "active_digest": head.active_manifest_digest
        if head
        else state.active_manifest_digest,
        "build": settings.RELEASE_BUILD_SHA or "development",
    }
    header = (
        freeze_delivery_authority(**values)
        if head
        else freeze_field_authority(
            **values,
            mode=state.mode,
            purpose="ai.run",
            fields=tuple(name for name, field in USER_FIELDS.items() if field.readable),
        )
    )
    return with_sources(
        header,
        (
            source_domain(
                action_id="delivery.message-read",
                object_id="ai.message",
                records=(message_id,),
                read=(("content", "plain"),),
            ),
        ),
    )
