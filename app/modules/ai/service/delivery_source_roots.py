"""Only the server owner can mint input roots and inherit actual model context."""

from sqlalchemy import select

from app.core.exceptions import BusinessException
from app.modules.ai.models.message import AiMessage
from app.modules.ai.service.delivery_source_domains import source_domain, with_sources
from app.modules.ai.service.field_authority import denied
from app.modules.ai.service.field_authority_runtime import ai_field_authority


async def mint_source(
    db,
    tx,
    *,
    tenant,
    version,
    identifier,
    conversation_id,
    dependencies=(),
    parts=False,
):
    groups = [
        source_domain(
            action_id="delivery.message-read",
            object_id="ai.message",
            records=(identifier,),
            read=tuple(
                (name, "plain")
                for name in (("content", "parts") if parts else ("content",))
            ),
        )
    ]
    if len(dependencies) > 4096:
        raise denied("FIELD_RESOURCE_LIMIT")
    users = list(
        await db.scalars(
            select(AiMessage)
            .where(
                AiMessage.tenant_id == tenant.tenant_id,
                AiMessage.conversation_id == conversation_id,
                AiMessage.role == "user",
                AiMessage.is_active.is_(True),
            )
            .order_by(AiMessage.message_id)
            .limit(4097)
        )
    )
    if len(users) > 4096:
        raise denied("FIELD_RESOURCE_LIMIT")
    for row in users:
        try:
            proof = await ai_field_authority.renew(
                db,
                tx,
                tenant=tenant,
                actor_user_version=version,
                frozen=row.field_authority,
            )
        except BusinessException:
            continue  # Protected history emits a tombstone for this same row.
        groups.extend(proof["source_domains"])
    for start in range(0, len(dependencies), 256):
        expected = set(dependencies[start : start + 256])
        rows = list(
            await db.scalars(
                select(AiMessage).where(
                    AiMessage.tenant_id == tenant.tenant_id,
                    AiMessage.conversation_id == conversation_id,
                    AiMessage.message_id.in_(expected),
                    AiMessage.role == "assistant",
                    AiMessage.is_active.is_(True),
                )
            )
        )
        if {row.message_id for row in rows} != expected:
            raise denied()
        for row in rows:
            proof = await ai_field_authority.renew(
                db,
                tx,
                tenant=tenant,
                actor_user_version=version,
                frozen=row.field_authority,
            )
            groups.extend(proof["source_domains"])
    return await ai_field_authority.mint(db, tx, tenant=tenant, domains=groups)


async def renew_source(db, tx, *, tenant, version, source):
    return await ai_field_authority.renew(
        db, tx, tenant=tenant, actor_user_version=version, frozen=source.field_authority
    )


def extend_source(root, *groups):
    return with_sources(root, tuple(groups))
