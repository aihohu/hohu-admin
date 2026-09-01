"""Conversation projection and deletion authorization invariants."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext
from app.modules.ai.api.conversation import delete_conversation
from app.modules.ai.service.conversation_service import conversation_service


def _user(user_id: int = 7001):
    return SimpleNamespace(
        user_id=user_id,
        _tenant_context=TenantContext(
            tenant_id=0,
            tenant_code="default",
            actor_user_id=user_id,
            tenant_version=1,
            source="access_token",
        ),
    )


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
    user = _user()
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
            "app.modules.ai.service.conversation_service.result_projection_service.authorize_message_projection",
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
    user = _user()
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
            "app.modules.ai.service.conversation_service.result_projection_service.authorize_message_projection",
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


async def test_delete_conversation_terminalizes_actions_before_delete() -> None:
    db = AsyncMock()
    current_user = _user()
    expired = [SimpleNamespace(confirmation_id="cid-1")]

    with (
        patch(
            "app.modules.ai.api.conversation.conversation_service.lock_for_delete",
            AsyncMock(),
        ) as lock_for_delete,
        patch(
            "app.modules.ai.api.conversation.prepared_action_service.expire_for_conversation_delete",
            AsyncMock(return_value=expired),
        ) as expire,
        patch(
            "app.modules.ai.api.conversation.conversation_service.delete",
            AsyncMock(),
        ) as delete,
    ):
        response = await delete_conversation(8001, db, current_user)

    assert response.code == 200
    lock_for_delete.assert_awaited_once_with(db, 8001, 7001)
    expire.assert_awaited_once_with(
        db,
        conversation_id=8001,
        user_id=7001,
        tenant_id=0,
    )
    delete.assert_awaited_once_with(db, 8001, 7001)
    db.commit.assert_awaited_once()


async def test_delete_conversation_does_not_commit_when_action_is_running() -> None:
    db = AsyncMock()
    current_user = _user()
    error = BusinessRuleException(
        "action is running",
        error_code="AI_ACTION_RUNNING",
    )
    error.code = 409

    with (
        patch(
            "app.modules.ai.api.conversation.conversation_service.lock_for_delete",
            AsyncMock(),
        ),
        patch(
            "app.modules.ai.api.conversation.prepared_action_service.expire_for_conversation_delete",
            AsyncMock(side_effect=error),
        ),
        patch(
            "app.modules.ai.api.conversation.conversation_service.delete",
            AsyncMock(),
        ) as delete,
    ):
        with pytest.raises(BusinessRuleException) as exc_info:
            await delete_conversation(8001, db, current_user)

    assert exc_info.value.error_code == "AI_ACTION_RUNNING"
    assert exc_info.value.code == 409
    delete.assert_not_awaited()
    db.commit.assert_not_awaited()
