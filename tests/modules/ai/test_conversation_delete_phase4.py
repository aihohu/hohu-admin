"""Phase 4 soft-delete and confirmation fencing contracts."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.exceptions import NotFoundException
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.ai.service.prepared_action_service import (
    canonical_payload_hash,
    prepared_action_service,
)
from app.modules.system.models.user import User


async def test_conversation_delete_is_soft_and_hidden_from_owner_reads(
    db_session,
    auth_token,
) -> None:
    assert auth_token
    owner = (
        await db_session.execute(select(User).where(User.user_name == "admin"))
    ).scalar_one()
    conversation = AiConversation(user_id=owner.user_id, title="phase4 soft delete")
    db_session.add(conversation)
    await db_session.flush()

    await conversation_service.delete(
        db_session,
        conversation.conversation_id,
        owner.user_id,
    )
    await db_session.flush()

    assert conversation.deleted_at is not None
    assert (
        await db_session.get(AiConversation, conversation.conversation_id) is not None
    )
    try:
        await conversation_service.get_by_id(
            db_session,
            conversation.conversation_id,
            owner.user_id,
        )
    except NotFoundException as exc:
        assert exc.error_code == "AI_CONVERSATION_NOT_FOUND"
    else:
        raise AssertionError("soft-deleted conversation remained owner-readable")


async def test_confirmation_context_ignores_soft_deleted_conversation(
    db_session,
    auth_token,
) -> None:
    assert auth_token
    owner = (
        await db_session.execute(select(User).where(User.user_name == "admin"))
    ).scalar_one()
    conversation = AiConversation(user_id=owner.user_id, title="phase4 confirm fence")
    db_session.add(conversation)
    await db_session.flush()
    source = AiMessage(
        conversation_id=conversation.conversation_id,
        role="user",
        message_type="text",
        content="phase4 source",
    )
    db_session.add(source)
    await db_session.flush()
    snapshot = {"tool": "user.update", "target": str(owner.user_id)}
    action = await prepared_action_service.create_pending(
        db_session,
        confirmation_id="cid_phase4_soft_delete",
        prepare_tool_call_id=None,
        prepare_tool_name=None,
        execute_tool_call_id="tc_phase4_soft_delete",
        execute_tool_name="user.update",
        frozen_args={"user_id": owner.user_id, "nickname": "never executed"},
        snapshot=snapshot,
        snapshot_hash=canonical_payload_hash(snapshot),
        subject_ref={"type": "user", "id": str(owner.user_id)},
        presentation={"title": "Update user", "fields": [], "warnings": []},
        interaction_flow="direct",
        requested_outcome="direct",
        user_id=owner.user_id,
        tenant_id=0,
        conversation_id=conversation.conversation_id,
        source_user_message_id=source.message_id,
        trace_id="tr_phase4_soft_delete",
        agent_code="user_mgmt",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        resolved_model_id=1,
        resolved_provider_id=1,
    )
    conversation.deleted_at = datetime.now(UTC)
    await db_session.flush()

    context = await prepared_action_service.lock_confirmation_context(
        db_session,
        confirmation_id=action.confirmation_id,
    )

    assert context is None
