"""Conversation projection and deletion authorization invariants."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import BusinessRuleException
from app.modules.ai.api.conversation import delete_conversation
from app.modules.ai.service.conversation_service import conversation_service


def _message(
    *,
    message_id: int,
    role: str,
    content: str,
    tool_calls: list[dict] | None = None,
):
    return SimpleNamespace(
        message_id=message_id,
        conversation_id=8001,
        parent_message_id=None,
        role=role,
        message_type="text",
        content=content,
        parts=None,
        tokens_input=None,
        tokens_output=None,
        tool_calls=tool_calls,
        trace_id=None,
        is_active=True,
        supersedes_message_id=None,
        create_time=datetime(2026, 8, 15, 9, 0),
        tenant_id=None,
        agent_code=None,
        tool_codes=None,
        subject_refs=None,
        subject_refs_hash=None,
        data_scope_hash=None,
        resolver_version=None,
    )


async def test_history_keeps_user_order_and_tombstones_denied_assistant() -> None:
    user = SimpleNamespace(user_id=7001)
    messages = [
        _message(message_id=1, role="user", content="show users"),
        _message(message_id=2, role="assistant", content="secret result"),
    ]
    with (
        patch.object(
            conversation_service,
            "get_messages",
            AsyncMock(return_value=messages),
        ),
        patch(
            "app.modules.ai.service.conversation_service.result_projection_service.authorize_result_projection",
            AsyncMock(return_value=False),
        ),
    ):
        projected = await conversation_service.project_messages(
            AsyncMock(),
            conversation_id=8001,
            current_user=user,
        )

    assert [item.role for item in projected] == ["user", "assistant"]
    assert projected[0].content == "show users"
    assert projected[1].status == "redacted"
    assert projected[1].error_code == "AI_RESULT_PROJECTION_FORBIDDEN"
    assert not hasattr(projected[1], "content")


async def test_authorized_history_refreshes_short_lived_download_url() -> None:
    user = SimpleNamespace(user_id=7001)
    message = _message(
        message_id=2,
        role="assistant",
        content="export ready",
        tool_calls=[{"ui": {"downloadUrl": "expired"}}],
    )
    lineage = SimpleNamespace(agent_code="user_mgmt")
    refreshed = [{"ui": {"downloadUrl": "fresh"}}]
    with (
        patch.object(
            conversation_service,
            "get_messages",
            AsyncMock(return_value=[message]),
        ),
        patch(
            "app.modules.ai.service.conversation_service.result_projection_service.lineage_from_record",
            return_value=lineage,
        ),
        patch(
            "app.modules.ai.service.conversation_service.result_projection_service.authorize_result_projection",
            AsyncMock(return_value=True),
        ),
        patch(
            "app.modules.ai.service.conversation_service.result_projection_service.refresh_download_urls",
            AsyncMock(return_value=refreshed),
        ) as refresh,
    ):
        projected = await conversation_service.project_messages(
            AsyncMock(),
            conversation_id=8001,
            current_user=user,
        )

    assert projected[0].tool_calls == refreshed
    refresh.assert_awaited_once()


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
