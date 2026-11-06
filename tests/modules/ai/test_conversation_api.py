"""Conversation projection and deletion authorization invariants."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, update

from app.core.tenant import TenantContext
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.conversation_service import conversation_service
from tests.modules.ai.test_authorization_conversation_http import (
    chat_model_case as chat_model_case,
)
from tests.modules.ai.test_authorization_conversation_http import (
    conversation_http as conversation_http,
)
from tests.modules.ai.test_authorization_conversation_http import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_conversation_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.ai.test_authorization_conversation_http import (
    recovery_case as recovery_case,
)
from tests.modules.ai.test_authorization_conversation_http import (
    system_sessions as system_sessions,
)


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


async def test_delete_conversation_terminalizes_actions_before_delete(
    conversation_http,
):
    client, plain, case = conversation_http
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                status="pending_confirmation",
                row_version=1,
                approved_by=None,
                approved_at=None,
                execution_owner=None,
                execution_lease_expires_at=None,
            )
        )
        await db.execute(
            update(AiOperationLog).values(
                status="pending_confirmation", approved_by=None, started_at=None
            )
        )
        await db.commit()
    response = await client.delete(f"/ai/conversation/{case.claim.conversation_id}")
    assert response.status_code == 200, response.text
    async with plain() as db:
        action = await db.scalar(select(AiPreparedAction))
        log = await db.scalar(select(AiOperationLog))
        conversation = await db.get(AiConversation, case.claim.conversation_id)
        assert action.status == "expired" and log.status == "expired"
        assert action.finished_at is not None and conversation.deleted_at is not None


async def test_delete_conversation_does_not_commit_when_action_is_running(
    conversation_http,
):
    client, plain, case = conversation_http
    response = await client.delete(f"/ai/conversation/{case.claim.conversation_id}")
    assert response.status_code == 409, response.text
    assert response.json()["errorCode"] == "AI_ACTION_RUNNING"
    async with plain() as db:
        assert (
            await db.get(AiConversation, case.claim.conversation_id)
        ).deleted_at is None
        assert (await db.scalar(select(AiPreparedAction))).status == "running"
