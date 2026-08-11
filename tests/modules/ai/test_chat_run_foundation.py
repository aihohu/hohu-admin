"""Task 35a.0: ChatCommand 因果、收口和 conversation guard 基础测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.id_generator import next_id
from app.modules.ai.agents.hitl.events import (
    DoneEvent,
    ToolCallResultEvent,
    ToolCallStartedEvent,
    event_to_sse_data,
)
from app.modules.ai.agents.hitl.manager import PendingPayload, hitl_manager
from app.modules.ai.api.chat import (
    _finalize_stream_turn,
    _run_guard_heartbeat_ttl,
)
from app.modules.ai.api.resume import _finalize_resume_terminal
from app.modules.ai.lifecycle import finalize_orphaned_pending
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.schemas.chat import resolve_chat_trace_id
from app.modules.ai.schemas.message import MessageOut
from app.modules.ai.service.chat_run_service import (
    ToolCallCollector,
    chat_run_finalizer,
    chat_run_guard,
)
from app.modules.ai.service.chat_service import chat_service
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.system.models.user import User


async def _create_conversation(db_session, *, suffix: str) -> AiConversation:
    user_id = next_id()
    user = User(
        user_id=user_id,
        user_name=f"chat_foundation_{suffix}_{user_id}",
        nickname=f"chat foundation {suffix}",
        hashed_password="$2b$12$dummy",
        status="1",
    )
    db_session.add(user)
    await db_session.flush()
    conversation = AiConversation(
        conversation_id=next_id(),
        user_id=user_id,
        title=f"foundation {suffix}",
    )
    db_session.add(conversation)
    await db_session.flush()
    return conversation


class TestStableTrace:
    def test_client_trace_is_validated_and_preserved(self) -> None:
        trace_id = "tr_0123456789abcdef0123456789abcdef"

        assert resolve_chat_trace_id(trace_id) == trace_id

    def test_missing_trace_gets_server_compatible_value(self) -> None:
        trace_id = resolve_chat_trace_id(None)

        assert trace_id.startswith("tr_")
        assert len(trace_id) == 35

    @pytest.mark.parametrize(
        "value",
        ["", "tr_short", "0123456789abcdef0123456789abcdef", "tr_" + "z" * 32],
    )
    def test_invalid_client_trace_is_rejected(self, value: str) -> None:
        with pytest.raises(Exception) as exc_info:
            resolve_chat_trace_id(value)

        assert getattr(exc_info.value, "error_code", None) == "AI_CHAT_TRACE_CONFLICT"


@pytest.mark.asyncio
async def test_user_message_flushes_source_and_exposes_trace(db_session) -> None:
    conversation = await _create_conversation(db_session, suffix="source")
    trace_id = "tr_11111111111111111111111111111111"

    message = await chat_service.save_user_message(
        db_session,
        conversation.conversation_id,
        conversation.user_id,
        "hello",
        agent_code="user_mgmt",
        trace_id=trace_id,
    )

    assert message.message_id is not None
    assert message.trace_id == trace_id
    output = MessageOut.model_validate(message).model_dump(by_alias=True)
    assert output["traceId"] == trace_id


@pytest.mark.asyncio
async def test_tool_only_turn_finalizes_once_and_keeps_started_order(
    db_session,
) -> None:
    conversation = await _create_conversation(db_session, suffix="finalize")
    trace_id = "tr_22222222222222222222222222222222"
    source = await chat_service.save_user_message(
        db_session,
        conversation.conversation_id,
        conversation.user_id,
        "run tools",
        agent_code="user_mgmt",
        trace_id=trace_id,
    )
    tool_calls = [
        {"tool": "user.first", "tool_call_id": "tc_first", "ok": True},
        {"tool": "user.second", "tool_call_id": "tc_second", "ok": True},
    ]

    first = await chat_run_finalizer.finalize_assistant_turn(
        db_session,
        conversation_id=conversation.conversation_id,
        trace_id=trace_id,
        source_user_message_id=source.message_id,
        content="",
        tool_calls=tool_calls,
        agent_code="user_mgmt",
    )
    second = await chat_run_finalizer.finalize_assistant_turn(
        db_session,
        conversation_id=conversation.conversation_id,
        trace_id=trace_id,
        source_user_message_id=source.message_id,
        content="",
        tool_calls=[tool_calls[1]],
        agent_code="user_mgmt",
    )

    assert first is second
    assert first.role == "assistant"
    assert first.content == ""
    assert first.parent_message_id == source.message_id
    assert [item["tool_call_id"] for item in first.tool_calls] == [
        "tc_first",
        "tc_second",
    ]


@pytest.mark.asyncio
async def test_terminal_finalizer_skips_missing_source_binding(db_session) -> None:
    result = await chat_run_finalizer.finalize_assistant_turn(
        db_session,
        conversation_id=200,
        trace_id="tr_missing_source_0000000000000000",
        source_user_message_id=201,
        content="",
        tool_calls=[{"tool": "test.missing", "tool_call_id": "tc_missing"}],
        agent_code="shared",
    )

    assert result is None


@pytest.mark.asyncio
async def test_terminal_finalizer_skips_inactive_source_binding(db_session) -> None:
    conversation = await _create_conversation(db_session, suffix="inactive")
    source = await chat_service.save_user_message(
        db_session,
        conversation.conversation_id,
        conversation.user_id,
        "superseded command",
        trace_id="tr_inactive_source_00000000000000",
    )
    source.is_active = False
    await db_session.flush()

    result = await chat_run_finalizer.finalize_assistant_turn(
        db_session,
        conversation_id=conversation.conversation_id,
        trace_id="tr_inactive_terminal_0000000000000",
        source_user_message_id=source.message_id,
        content="",
        tool_calls=[{"tool": "test.inactive", "tool_call_id": "tc_inactive"}],
        agent_code="shared",
    )

    assert result is None


@pytest.mark.asyncio
async def test_active_history_filters_and_orders_stably(db_session) -> None:
    conversation = await _create_conversation(db_session, suffix="history")
    trace_id = "tr_77777777777777777777777777777777"
    first = await chat_service.save_user_message(
        db_session,
        conversation.conversation_id,
        conversation.user_id,
        "first",
        trace_id=trace_id,
    )
    inactive = AiMessage(
        message_id=next_id(),
        conversation_id=conversation.conversation_id,
        role="assistant",
        content="inactive",
        trace_id=trace_id,
        is_active=False,
    )
    last = AiMessage(
        message_id=next_id(),
        conversation_id=conversation.conversation_id,
        role="assistant",
        content="last",
        trace_id="tr_88888888888888888888888888888888",
        is_active=True,
    )
    db_session.add_all([inactive, last])
    await db_session.flush()

    messages = await chat_service.load_history(
        db_session,
        conversation.conversation_id,
        conversation.user_id,
    )

    assert [message.message_id for message in messages] == [
        first.message_id,
        last.message_id,
    ]


@pytest.mark.asyncio
async def test_reused_trace_is_rejected_before_new_run(db_session) -> None:
    conversation = await _create_conversation(db_session, suffix="trace-conflict")
    trace_id = "tr_99999999999999999999999999999999"
    await chat_service.save_user_message(
        db_session,
        conversation.conversation_id,
        conversation.user_id,
        "first",
        trace_id=trace_id,
    )

    with pytest.raises(Exception) as exc_info:
        await chat_service.ensure_trace_available(
            db_session,
            conversation_id=conversation.conversation_id,
            trace_id=trace_id,
        )

    assert getattr(exc_info.value, "error_code", None) == "AI_CHAT_TRACE_CONFLICT"


def test_done_event_carries_durability_ack() -> None:
    payload = event_to_sse_data(
        DoneEvent(
            trace_id="tr_33333333333333333333333333333333",
            message_id=9007199254740993,
            persistence="committed",
            projection="updated",
        )
    )

    assert '"traceId": "tr_33333333333333333333333333333333"' in payload
    assert '"messageId": "9007199254740993"' in payload
    assert '"persistence": "committed"' in payload
    assert '"projection": "updated"' in payload


def test_tool_calls_are_collected_in_started_order() -> None:
    collector = ToolCallCollector()
    collector.record(
        ToolCallStartedEvent(
            tool="user.first",
            tool_call_id="tc_first",
            summary="first",
            args={},
            risk="low",
            trace_id="tr_44444444444444444444444444444444",
        )
    )
    collector.record(
        ToolCallStartedEvent(
            tool="user.second",
            tool_call_id="tc_second",
            summary="second",
            args={},
            risk="low",
            trace_id="tr_44444444444444444444444444444444",
        )
    )
    collector.record(
        ToolCallResultEvent(
            tool="user.second",
            tool_call_id="tc_second",
            ok=True,
            duration_ms=2,
        )
    )
    collector.record(
        ToolCallResultEvent(
            tool="user.first",
            tool_call_id="tc_first",
            ok=True,
            duration_ms=3,
        )
    )

    assert [item["tool_call_id"] for item in collector.snapshot()] == [
        "tc_first",
        "tc_second",
    ]


@pytest.mark.asyncio
async def test_stream_finalizer_commits_before_building_done_ack() -> None:
    order: list[str] = []
    db = AsyncMock()
    db.commit.side_effect = lambda: order.append("commit")

    async def _finalize(*_args, **_kwargs):
        order.append("finalize")
        return SimpleNamespace(message_id=9007199254740993)

    with patch.object(
        chat_run_finalizer,
        "finalize_assistant_turn",
        side_effect=_finalize,
    ):
        terminal_events = await _finalize_stream_turn(
            db,
            conversation_id=123,
            trace_id="tr_55555555555555555555555555555555",
            source_user_message_id=456,
            content="",
            tool_calls=[{"tool_call_id": "tc_1", "ok": True}],
            agent_code="user_mgmt",
            stream_error_code=None,
        )

    assert order == ["finalize", "commit"]
    assert terminal_events == [
        DoneEvent(
            trace_id="tr_55555555555555555555555555555555",
            message_id=9007199254740993,
            persistence="committed",
            projection="updated",
        )
    ]


class TestConversationRunGuard:
    def test_pending_heartbeat_preserves_confirmation_window(self) -> None:
        assert _run_guard_heartbeat_ttl(pending_handoff=True) >= 360

    @pytest.mark.asyncio
    async def test_only_owner_can_renew_and_release(self) -> None:
        redis = AsyncMock()
        redis.set.return_value = True
        redis.eval.side_effect = [1, 0, 0, 1]

        acquired = await chat_run_guard.acquire(
            redis,
            conversation_id=123,
            owner_token="owner-a",
            ttl_sec=60,
        )
        renewed = await chat_run_guard.renew(
            redis,
            conversation_id=123,
            owner_token="owner-a",
            ttl_sec=60,
        )
        wrong_renew = await chat_run_guard.renew(
            redis,
            conversation_id=123,
            owner_token="owner-b",
            ttl_sec=60,
        )
        wrong_release = await chat_run_guard.release(
            redis,
            conversation_id=123,
            owner_token="owner-b",
        )
        released = await chat_run_guard.release(
            redis,
            conversation_id=123,
            owner_token="owner-a",
        )

        assert acquired is True
        assert renewed is True
        assert wrong_renew is False
        assert wrong_release is False
        assert released is True

    @pytest.mark.asyncio
    async def test_pending_handoff_outlives_confirmation_window(self) -> None:
        redis = AsyncMock()
        redis.eval.return_value = 1

        extended = await chat_run_guard.handoff_pending(
            redis,
            conversation_id=123,
            owner_token="owner-a",
            confirmation_ttl_sec=300,
        )

        assert extended is True
        assert redis.eval.await_args.args[-1] >= 360


@pytest.mark.asyncio
async def test_startup_cleanup_commits_before_owned_guard_release() -> None:
    order: list[str] = []
    db = AsyncMock()
    redis = AsyncMock()
    db.commit.side_effect = lambda: order.append("commit")
    pending = PendingPayload(
        user_id=1,
        tenant_id=0,
        conversation_id=123,
        tool_call_id="tc_startup",
        trace_id="tr_66666666666666666666666666666666",
        tool_name="user.update",
        args={},
        dry_run_result=None,
        expires_at="2026-08-07T00:00:00Z",
        source_user_message_id=456,
        guard_owner_token="owner-a",
    )
    operation_log = SimpleNamespace(
        log_id=789,
        status="pending_confirmation",
        duration_ms=None,
        result_summary=None,
        error_code=None,
    )

    async def _mark(*_args, **_kwargs):
        order.append("operation")

    async def _finalize(*_args, **_kwargs):
        order.append("finalizer")

    async def _release(*_args, **_kwargs):
        order.append("release")
        return True

    async def _delete(*_args, **_kwargs):
        order.append("delete")

    with (
        patch.object(
            operation_log_service, "mark_expired_if_pending", side_effect=_mark
        ),
        patch.object(
            chat_run_finalizer, "finalize_pending_turn", side_effect=_finalize
        ),
        patch.object(chat_run_guard, "release", side_effect=_release),
        patch.object(hitl_manager, "delete_pending", side_effect=_delete),
    ):
        await finalize_orphaned_pending(
            db,
            redis,
            confirmation_id="conf-startup",
            pending=pending,
            operation_log=operation_log,
        )

    assert order == ["operation", "finalizer", "commit", "release", "delete"]


@pytest.mark.asyncio
async def test_startup_cleanup_does_not_overwrite_committed_terminal_message() -> None:
    order: list[str] = []
    db = AsyncMock()
    redis = AsyncMock()
    db.scalar.return_value = 999
    db.commit.side_effect = lambda: order.append("commit")
    pending = PendingPayload(
        user_id=1,
        tenant_id=0,
        conversation_id=123,
        tool_call_id="tc_terminal",
        trace_id="tr_88888888888888888888888888888888",
        tool_name="user.update",
        args={},
        dry_run_result=None,
        expires_at="2026-08-07T00:00:00Z",
        source_user_message_id=456,
        guard_owner_token="owner-a",
    )
    operation_log = SimpleNamespace(
        log_id=789,
        status="success",
        duration_ms=10,
        result_summary="ok",
        error_code=None,
    )

    async def _release(*_args, **_kwargs):
        order.append("release")
        return True

    async def _delete(*_args, **_kwargs):
        order.append("delete")

    with (
        patch.object(
            chat_run_finalizer, "finalize_pending_turn", AsyncMock()
        ) as finalize,
        patch.object(chat_run_guard, "release", side_effect=_release),
        patch.object(hitl_manager, "delete_pending", side_effect=_delete),
    ):
        await finalize_orphaned_pending(
            db,
            redis,
            confirmation_id="conf-terminal",
            pending=pending,
            operation_log=operation_log,
        )

    finalize.assert_not_awaited()
    assert order == ["commit", "release", "delete"]


@pytest.mark.asyncio
async def test_resume_terminal_commits_before_guard_release_and_pending_delete() -> (
    None
):
    order: list[str] = []
    db = AsyncMock()
    db.commit.side_effect = lambda: order.append("commit")
    pending = PendingPayload(
        user_id=1,
        tenant_id=0,
        conversation_id=123,
        tool_call_id="tc_resume",
        trace_id="tr_77777777777777777777777777777777",
        tool_name="user.update",
        args={},
        dry_run_result=None,
        expires_at="2026-08-07T00:00:00Z",
        source_user_message_id=456,
        guard_owner_token="owner-a",
    )

    async def _finalize(*_args, **_kwargs):
        order.append("finalizer")
        return SimpleNamespace(message_id=789)

    async def _release(*_args, **_kwargs):
        order.append("release")
        return True

    async def _delete(*_args, **_kwargs):
        order.append("delete")

    with (
        patch.object(
            chat_run_finalizer, "finalize_pending_turn", side_effect=_finalize
        ),
        patch.object(chat_run_guard, "release", side_effect=_release),
        patch.object(hitl_manager, "delete_pending", side_effect=_delete),
    ):
        events = await _finalize_resume_terminal(
            db,
            confirmation_id="conf-resume",
            pending=pending,
            ok=True,
        )

    assert order == ["finalizer", "commit", "release", "delete"]
    assert events == [
        DoneEvent(
            trace_id=pending.trace_id,
            message_id=789,
            persistence="committed",
            projection="updated",
        )
    ]
