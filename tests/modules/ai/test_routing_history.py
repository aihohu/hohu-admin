"""Routing history must be bounded, server-owned user intent, never tool output."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from tenant_helpers import tenant_context

from app.core.exceptions import NotFoundException
from app.core.id_generator import next_id
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.system.models.user import User


@pytest.fixture
async def routing_conversation(db_session):
    admin = await db_session.scalar(
        select(User).where(User.tenant_id == 0, User.user_name == "admin")
    )
    conversation = AiConversation(
        tenant_id=0, user_id=admin.user_id, title="routing history regression"
    )
    db_session.add(conversation)
    await db_session.flush()
    return conversation, tenant_context(tenant_id=0, actor_user_id=admin.user_id)


def add_message(db, conversation, content, *, role="user", active=True):
    db.add(
        AiMessage(
            message_id=next_id(),
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.conversation_id,
            role=role,
            message_type="text",
            content=content,
            is_active=active,
        )
    )


async def test_history_contains_only_recent_active_user_turns(
    db_session, routing_conversation
):
    conversation, tenant = routing_conversation
    for i in range(8):
        add_message(db_session, conversation, f"question-{i}")
    add_message(db_session, conversation, "private tool result", role="assistant")
    add_message(db_session, conversation, "private system prompt", role="system")
    add_message(db_session, conversation, "raw tool output", role="tool")
    add_message(db_session, conversation, "superseded question", active=False)
    await db_session.flush()

    history = await conversation_service.get_routing_history(
        db_session, conversation.conversation_id, tenant.actor_user_id, tenant=tenant
    )

    assert history == [f"question-{i}" for i in range(2, 8)]


async def test_history_redacts_before_truncating_without_mutating_messages(
    db_session, routing_conversation
):
    conversation, tenant = routing_conversation
    secret = "sk-" + "a" * 30
    original = "x" * 985 + secret + "y" * 100
    add_message(db_session, conversation, original)
    await db_session.flush()

    history = await conversation_service.get_routing_history(
        db_session, conversation.conversation_id, tenant.actor_user_id, tenant=tenant
    )

    assert len(history) == 1
    assert len(history[0]) <= 1000
    assert "sk-" not in history[0]
    assert "[REDACTED:" in history[0]
    persisted = await db_session.scalar(
        select(AiMessage.content).where(
            AiMessage.tenant_id == tenant.tenant_id,
            AiMessage.conversation_id == conversation.conversation_id,
        )
    )
    assert persisted == original


@pytest.mark.parametrize("boundary", ["tenant", "owner", "deleted"])
async def test_history_rejects_foreign_or_deleted_conversation(
    db_session, routing_conversation, boundary
):
    conversation, tenant = routing_conversation
    actor_id = tenant.actor_user_id
    if boundary == "tenant":
        tenant = tenant_context(tenant_id=next_id(), actor_user_id=actor_id)
    elif boundary == "owner":
        actor_id = next_id()
    else:
        conversation.deleted_at = datetime.now(UTC)
        await db_session.flush()

    with pytest.raises(NotFoundException):
        await conversation_service.get_routing_history(
            db_session, conversation.conversation_id, actor_id, tenant=tenant
        )
