"""Current message fields cap every original-input and generated-text reader."""

from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import _Journal
from app.modules.ai.schemas.message import MessageOut
from app.modules.ai.service.delivery_source_domains import validate_receipt
from app.modules.ai.service.field_authority_runtime import ai_field_authority
from app.modules.system.service.delivery_read_context import load_delivery_context


class MessageDelivery:
    def __init__(self, context, authority, db, tx):
        self.context = context
        self.authority = authority
        self.db, self.tx = db, tx

    def decisions(self, message):
        return self.context.decisions(
            self.context.record(
                "ai.message", str(message.message_id), self.context.scopes
            )
        )

    async def body_visible(self, message):
        if (
            not self.decisions(message)["content"].allowed
            or validate_receipt(message.field_authority) is None
        ):
            return False
        try:
            await ai_field_authority.renew(
                self.db,
                self.tx,
                tenant=self.context.tenant,
                actor_user_version=self.context.actor.auth_version,
                frozen=message.field_authority,
            )
        except BusinessException:
            return False
        return True

    async def project(self, message):
        values = MessageOut.model_validate(message).model_dump()
        # Usage counters are execution internals, not business message content.
        for name in ("tokens_input", "tokens_output"):
            values.pop(name, None)
        decisions = self.decisions(message)
        for name in ("content", "parts", "tool_calls", "create_time"):
            if not decisions[name].allowed:
                values.pop(name, None)
        if values.get("parts"):
            from app.modules.ai.service.delivery_file_parts import (  # noqa: PLC0415
                normalize_parts,  # noqa: PLC0415
            )

            values["parts"], _ = await normalize_parts(
                self.db,
                self.tx,
                tenant=self.context.tenant,
                version=self.context.actor.auth_version,
                parts=values["parts"],
            )
        return MessageOut.model_validate(values)


async def message_delivery(db, *, actor, tenant):
    journal = getattr(db, "_authorization_journal", None)
    tx = journal.handle if isinstance(journal, _Journal) else None
    if tx is None:
        return None
    try:
        context = await load_delivery_context(
            db,
            tx,
            tenant=tenant,
            version=actor.auth_version,
            action="delivery.message-read",
        )
        authority = await ai_field_authority.current(db, tx, tenant=tenant)
    except BusinessException:
        return None
    return MessageDelivery(context, authority, db, tx)
