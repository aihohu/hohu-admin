"""Conversation deletion must preserve durable prepared-action invariants."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import BusinessRuleException
from app.modules.ai.api.conversation import delete_conversation


async def test_delete_conversation_rejects_in_progress_prepared_action() -> None:
    db = AsyncMock()
    current_user = MagicMock(user_id=7001)

    with (
        patch(
            "app.modules.ai.api.conversation.conversation_service.lock_for_delete",
            AsyncMock(),
        ) as lock_for_delete,
        patch(
            "app.modules.ai.api.conversation.prepared_action_service.has_in_progress_for_conversation",
            AsyncMock(return_value=True),
        ),
        patch(
            "app.modules.ai.api.conversation.conversation_service.delete",
            AsyncMock(),
        ) as delete,
    ):
        with pytest.raises(BusinessRuleException) as exc_info:
            await delete_conversation(8001, db, current_user)

    assert exc_info.value.error_code == "AI_CHAT_RUN_IN_PROGRESS"
    assert exc_info.value.code == 409
    lock_for_delete.assert_awaited_once_with(db, 8001, 7001)
    delete.assert_not_awaited()
    db.commit.assert_not_awaited()
