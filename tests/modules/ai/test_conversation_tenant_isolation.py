"""Dual-tenant regression tests for the AI conversation aggregate."""

import pytest
from sqlalchemy import func, select
from tenant_helpers import create_test_tenant, tenant_context

from app.core.exceptions import NotFoundException
from app.core.id_generator import next_id
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.schemas.conversation import ConversationQuery, ConversationUpdate
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.system.models.user import User


async def test_conversation_reads_and_writes_are_tenant_scoped(db_session) -> None:
    tenant_b = await create_test_tenant(db_session, prefix="ai-conv-b")
    user_a = (
        await db_session.execute(
            select(User).where(User.tenant_id == 0, User.user_name == "admin")
        )
    ).scalar_one()
    marker = next_id()
    user_b = User(
        tenant_id=tenant_b.tenant_id,
        user_name=f"ai-conversation-user-{marker}",
        nickname="Tenant B AI user",
        hashed_password="x",
        status="1",
    )
    db_session.add(user_b)
    await db_session.flush()

    shared_title = f"shared-conversation-{marker}"
    conversation_a = AiConversation(
        tenant_id=0,
        user_id=user_a.user_id,
        title=shared_title,
        model_name="legacy-model",
    )
    conversation_b = AiConversation(
        tenant_id=tenant_b.tenant_id,
        user_id=user_b.user_id,
        title=shared_title,
        model_name="legacy-model",
    )
    db_session.add_all([conversation_a, conversation_b])
    await db_session.flush()

    tenant_a_ctx = tenant_context(tenant_id=0, actor_user_id=user_a.user_id)
    page = await conversation_service.get_list(
        db_session,
        ConversationQuery(title=shared_title),
        user_a.user_id,
        tenant=tenant_a_ctx,
    )

    assert [row.conversation_id for row in page.records] == [
        conversation_a.conversation_id
    ]

    missing_id = next_id()
    for conversation_id in (conversation_b.conversation_id, missing_id):
        with pytest.raises(NotFoundException) as exc_info:
            await conversation_service.get_by_id(
                db_session,
                conversation_id,
                user_a.user_id,
                tenant=tenant_a_ctx,
            )
        assert exc_info.value.error_code == "AI_CONVERSATION_NOT_FOUND"

    before_messages = await db_session.scalar(
        select(func.count(AiMessage.message_id)).where(
            AiMessage.tenant_id == tenant_b.tenant_id,
            AiMessage.conversation_id == conversation_b.conversation_id,
        )
    )
    with pytest.raises(NotFoundException):
        await conversation_service.update(
            db_session,
            conversation_b.conversation_id,
            ConversationUpdate(title="must-not-change"),
            user_a.user_id,
            tenant=tenant_a_ctx,
        )
    with pytest.raises(NotFoundException):
        await conversation_service.delete(
            db_session,
            conversation_b.conversation_id,
            user_a.user_id,
            tenant=tenant_a_ctx,
        )
    with pytest.raises(NotFoundException):
        await conversation_service.save_message(
            db_session,
            conversation_b.conversation_id,
            "user",
            "must-not-persist",
            tenant=tenant_a_ctx,
        )
    after_messages = await db_session.scalar(
        select(func.count(AiMessage.message_id)).where(
            AiMessage.tenant_id == tenant_b.tenant_id,
            AiMessage.conversation_id == conversation_b.conversation_id,
        )
    )

    assert conversation_b.title == shared_title
    assert conversation_b.deleted_at is None
    assert after_messages == before_messages
