"""ChatCommand 因果、收口和 conversation guard 基础测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic.experimental.missing_sentinel import MISSING

from app.core.id_generator import next_id
from app.modules.ai.agents.hitl.events import (
    AiErrorEvent,
    DoneEvent,
    ToolCallResultEvent,
    ToolCallStartedEvent,
    event_to_sse_data,
)
from app.modules.ai.agents.hitl.manager import PendingPayload, hitl_manager
from app.modules.ai.agents.tools import load_builtin_tools
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
    enforce_grounded_management_write_claim,
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
        projection_dependency_message_ids=[11, 12],
    )
    second = await chat_run_finalizer.finalize_assistant_turn(
        db_session,
        conversation_id=conversation.conversation_id,
        trace_id=trace_id,
        source_user_message_id=source.message_id,
        content="",
        tool_calls=[tool_calls[1]],
        agent_code="user_mgmt",
        projection_dependency_message_ids=[99],
    )

    assert first is second
    assert first.role == "assistant"
    assert first.content == ""
    assert first.parent_message_id == source.message_id
    assert first.projection_dependency_message_ids == ["11", "12"]
    assert [item["tool_call_id"] for item in first.tool_calls] == [
        "tc_first",
        "tc_second",
    ]


@pytest.mark.asyncio
async def test_terminal_finalizer_omits_missing_sentinel_tool_args(db_session) -> None:
    conversation = await _create_conversation(db_session, suffix="sentinel")
    trace_id = "tr_missing_sentinel_000000000000000"
    source = await chat_service.save_user_message(
        db_session,
        conversation.conversation_id,
        conversation.user_id,
        "update role",
        agent_code="role_mgmt",
        trace_id=trace_id,
    )

    message = await chat_run_finalizer.finalize_assistant_turn(
        db_session,
        conversation_id=conversation.conversation_id,
        trace_id=trace_id,
        source_user_message_id=source.message_id,
        content="updated",
        tool_calls=[
            {
                "tool": "role.update",
                "tool_call_id": "tc_missing_sentinel",
                "args": {"role_id": 42, "role_desc": MISSING},
                "ok": True,
            }
        ],
        agent_code="role_mgmt",
    )

    assert message is not None
    assert message.tool_calls[0]["args"] == {"role_id": 42}


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

    current_user = await db_session.get(User, conversation.user_id)
    assert current_user is not None
    messages = await chat_service.load_history(
        db_session,
        conversation.conversation_id,
        current_user,
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


@pytest.mark.asyncio
async def test_stream_finalizer_replaces_ungrounded_management_write_claim() -> None:
    db = AsyncMock()
    captured: dict[str, object] = {}

    async def _finalize(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(message_id=9007199254740993)

    with patch.object(
        chat_run_finalizer,
        "finalize_assistant_turn",
        side_effect=_finalize,
    ):
        terminal_events = await _finalize_stream_turn(
            db,
            conversation_id=123,
            trace_id="tr_66666666666666666666666666666666",
            source_user_message_id=456,
            content="部门已成功创建，部门 ID 为 998877。",
            tool_calls=None,
            agent_code="dept_mgmt",
            stream_error_code=None,
        )

    assert captured["content"] == (
        "本轮未产生可验证的写工具结果，因此没有确认任何业务变更。请重新发起操作。"
    )
    assert terminal_events[0] == AiErrorEvent(
        error_code="AI_UNVERIFIED_WRITE_CLAIM",
        message="AI 回复缺少可验证的写工具结果，未确认任何业务变更",
    )
    assert isinstance(terminal_events[1], DoneEvent)


@pytest.mark.asyncio
async def test_stream_finalizer_redacts_provider_text_after_import_field_errors() -> (
    None
):
    db = AsyncMock()
    captured: dict[str, object] = {}
    secret_value = "private-invalid@example"
    raw_row = "x, 华东-销售组, 1"

    async def _finalize(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(message_id=9007199254740993)

    with patch.object(
        chat_run_finalizer,
        "finalize_assistant_turn",
        side_effect=_finalize,
    ):
        await _finalize_stream_turn(
            db,
            conversation_id=123,
            trace_id="tr_import_redaction_000000000000000",
            source_user_message_id=456,
            content=f"Invalid value {secret_value}; source row: {raw_row}",
            tool_calls=[
                {
                    "tool": "user.import_preview",
                    "tool_call_id": "tc_import_redaction",
                    "ok": False,
                    "error_code": "AI_IMPORT_FIELD_ERRORS",
                    "error_msg": (
                        "Import validation failed: row 2, user_email: "
                        "user_email format is invalid [AI_IMPORT_EMAIL_INVALID]"
                    ),
                }
            ],
            agent_code="user_mgmt",
            stream_error_code=None,
        )

    persisted = str(captured["content"])
    assert "行号、字段、原因和错误码" in persisted
    assert secret_value not in persisted
    assert raw_row not in persisted


def test_successful_write_tool_grounds_management_write_claim() -> None:
    load_builtin_tools()
    content = "部门已成功创建。"

    grounded_content, blocked = enforce_grounded_management_write_claim(
        content,
        agent_code="dept_mgmt",
        tool_calls=[{"tool": "dept.create", "ok": True}],
    )

    assert grounded_content == content
    assert blocked is False


def test_non_assertive_management_guidance_is_not_blocked() -> None:
    content = "填写部门名称后即可创建部门。"

    grounded_content, blocked = enforce_grounded_management_write_claim(
        content,
        agent_code="dept_mgmt",
        tool_calls=None,
    )

    assert grounded_content == content
    assert blocked is False


@pytest.mark.parametrize(
    ("action_status", "expected_blocked"),
    [("executed", False), ("previewed", True)],
)
def test_prepared_tool_only_grounds_an_executed_write(
    action_status: str,
    expected_blocked: bool,
) -> None:
    load_builtin_tools()

    grounded_content, blocked = enforce_grounded_management_write_claim(
        "用户已成功导入。",
        agent_code="user_mgmt",
        tool_calls=[
            {
                "tool": "user.import_preview",
                "ok": True,
                "result": {"actionStatus": action_status},
            }
        ],
    )

    assert blocked is expected_blocked
    assert (grounded_content == "用户已成功导入。") is (not expected_blocked)


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
