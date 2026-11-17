"""Owner fields and known title origins are required in addition to AI lineage."""

from sqlalchemy import and_, or_, select

from app.core.field_policy import validate_write
from app.core.field_policy.contracts import denied
from app.modules.ai.models.conversation import AiConversation
from app.modules.system.field_policy.delivery_catalog import CREATE, UPDATE
from app.modules.system.schemas.delivery_projection import ProjectedConversation
from app.modules.system.service.delivery_read_context import load_delivery_context


def safe_title(record):
    if record.title_origin == "authored":
        return record.title
    if record.title_origin == "fixed" and record.title == "新对话":
        return record.title
    return None


def known_title(table):
    return or_(
        table.c.title_origin == "authored",
        and_(table.c.title_origin == "fixed", table.c.title == "新对话"),
    )


async def require_known_title_query(db, context):
    table = AiConversation.__table__
    if await db.scalar(
        select(
            select(table.c.conversation_id)
            .where(context.base("ai.conversation", table), ~known_title(table))
            .exists()
        )
    ):
        raise denied()


async def validate_conversation_write(
    db, tx, *, tenant, version, identifier, data, create
):
    context = await load_delivery_context(
        db, tx, tenant=tenant, version=version, action="delivery.conversation-write"
    )
    fields = tuple(name for name in data if name in {"title", "status"})
    if fields:
        validate_write(
            context.catalog,
            context.rules,
            context.record("ai.conversation", str(identifier), context.scopes),
            context.action,
            CREATE if create else UPDATE,
            fields,
        )


async def validate_message_create(db, tx, *, tenant, version, identifier, parts=False):
    context = await load_delivery_context(
        db, tx, tenant=tenant, version=version, action="delivery.message-create"
    )
    validate_write(
        context.catalog,
        context.rules,
        context.record("ai.message", str(identifier), context.scopes),
        context.action,
        CREATE,
        ("content", "parts") if parts else ("content",),
    )
    # Sending original input to a model processes those fields as well as writing
    # them. A create-only permission does not license that separate use.
    await require_message_read(
        db,
        tx,
        tenant=tenant,
        version=version,
        identifier=identifier,
        names=("content", "parts") if parts else ("content",),
    )


async def project_conversation(db, tx, *, tenant, version, identifier):
    context = await load_delivery_context(
        db, tx, tenant=tenant, version=version, action="delivery.conversation-list"
    )
    rows = await context.rows(
        db,
        "ai.conversation",
        AiConversation,
        "conversation_id",
        predicates=(AiConversation.conversation_id == identifier,),
        maximum=1,
        internals=("model_name",),
    )
    if len(rows) != 1:
        raise denied()
    record, values, control = rows[0]
    reference = control["model_name"]
    if (
        type(reference) is str
        and reference.isascii()
        and reference.isdigit()
        and 0 < int(reference) < 2**63
    ):
        values["model_name"] = reference
    values["conversation_id"] = str(values["conversation_id"])
    values["field_policy"] = context.policy(record)
    return ProjectedConversation.model_validate(values)


async def require_message_read(
    db, tx, *, tenant, version, identifier, names=("content",)
):
    context = await load_delivery_context(
        db, tx, tenant=tenant, version=version, action="delivery.message-read"
    )
    record = context.record("ai.message", str(identifier), context.scopes)
    decisions = context.decisions(record)
    if any(name not in decisions or not decisions[name].allowed for name in names):
        raise denied()
    return context
