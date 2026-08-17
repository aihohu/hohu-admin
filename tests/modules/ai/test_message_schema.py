"""MessageOut schema 字段类型测试。

tool_calls 在 chat.py::_record_tool_event 中以 list[dict] 形式收集
（每个 tool 调用一个 dict），但旧 schema 写成 dict | None，导致 reload
含 tool_calls 的会话时 Pydantic ValidationError。
"""

# ruff: noqa: ARG001, PLC0415

from datetime import datetime

from app.modules.ai.models.message import AiMessage
from app.modules.ai.schemas.message import MessageOut, MessageTombstoneOut


def _make_msg(*, tool_calls: list | None) -> AiMessage:
    return AiMessage(
        message_id=1,
        conversation_id=1,
        parent_message_id=None,
        role="assistant",
        message_type="text",
        content="ok",
        parts=None,
        tokens_input=None,
        tokens_output=None,
        tool_calls=tool_calls,
        is_active=True,
        create_time=datetime(2026, 7, 17, 12, 0, 0),
    )


class TestMessageOutToolCallsList:
    """tool_calls 必须是 list[dict]：每次 tool 调用一个 dict，按顺序追加"""

    def test_none_tool_calls(self) -> None:
        """纯文本消息无 tool_calls"""
        msg = _make_msg(tool_calls=None)
        out = MessageOut.model_validate(msg)
        assert out.tool_calls is None

    def test_empty_list(self) -> None:
        """空 list 也合法（理论不会出现，但 schema 不应拒绝）"""
        msg = _make_msg(tool_calls=[])
        out = MessageOut.model_validate(msg)
        assert out.tool_calls == []

    def test_single_tool_call(self) -> None:
        """单次 tool 调用"""
        msg = _make_msg(
            tool_calls=[
                {
                    "tool": "user.count",
                    "tool_call_id": "tc_1",
                    "args": {"status": "1"},
                    "ok": True,
                    "result": {"count": 5},
                    "duration_ms": 100,
                }
            ]
        )
        out = MessageOut.model_validate(msg)
        assert isinstance(out.tool_calls, list)
        assert len(out.tool_calls) == 1
        assert out.tool_calls[0]["tool"] == "user.count"

    def test_multiple_tool_calls(self) -> None:
        """多次 tool 调用按顺序存储（典型场景：LLM 连续调多个 tool）"""
        msg = _make_msg(
            tool_calls=[
                {"tool": "user.count", "tool_call_id": "tc_1", "ok": True},
                {"tool": "user.batch_delete", "tool_call_id": "tc_2", "ok": True},
            ]
        )
        out = MessageOut.model_validate(msg)
        assert len(out.tool_calls) == 2
        assert out.tool_calls[0]["tool"] == "user.count"
        assert out.tool_calls[1]["tool"] == "user.batch_delete"

    def test_snowflake_id_in_args_is_string(self) -> None:
        """修 BUG 后 args 中的 Snowflake ID 已 stringify，reload 时保持 str"""
        msg = _make_msg(
            tool_calls=[
                {
                    "tool": "user.batch_delete",
                    "tool_call_id": "tc_1",
                    "args": {"user_ids": ["7483433649145122816"]},
                    "ok": True,
                    "result": {"deleted": 1, "user_ids": ["7483433649145122816"]},
                    "duration_ms": 13,
                }
            ]
        )
        out = MessageOut.model_validate(msg)
        assert out.tool_calls[0]["args"]["user_ids"] == ["7483433649145122816"]
        assert out.tool_calls[0]["result"]["user_ids"] == ["7483433649145122816"]

    def test_chip_target_and_ui_round_trip(self) -> None:
        """chat.py::_record_tool_event 持久化时必须包含 chip_target 和 ui，
        否则 reload 后 chip 跳转消失 + 卡片 fallback PlainJsonView. (C1 回归)"""
        msg = _make_msg(
            tool_calls=[
                {
                    "tool": "user.count",
                    "tool_call_id": "tc_1",
                    "summary": "count users",
                    "args": {"status": "1"},
                    "risk": "low",
                    "trace_id": "tr_abc",
                    "chip_target": "/system/user",
                    "ok": True,
                    "result": {"count": 5},
                    "affected_rows": None,
                    "duration_ms": 42,
                    "ui": {
                        "viewType": "rows_affected",
                        "viewData": {"count": 5},
                        "labelKey": "ai.tool.user.count.result",
                    },
                }
            ]
        )
        out = MessageOut.model_validate(msg)
        tc = out.tool_calls[0]
        assert tc["chip_target"] == "/system/user"
        assert tc["ui"]["viewType"] == "rows_affected"
        assert tc["ui"]["viewData"]["count"] == 5
        assert tc["ui"]["labelKey"] == "ai.tool.user.count.result"


def test_ai_message_has_agent_code_column():
    """ai_message.agent_code 记录本条消息实际处理的 Agent code。"""
    from app.modules.ai.models.message import AiMessage

    col = AiMessage.__table__.columns.get("agent_code")
    assert col is not None, "ai_message.agent_code 列必须存在"
    assert col.nullable is True, "agent_code 必须 nullable（历史消息可能没有）"
    assert str(col.type) == "VARCHAR(64)"


def test_ai_message_has_routing_feedback_column():
    """routing_feedback 可为 correct、wrong 或 null。"""
    from app.modules.ai.models.message import AiMessage

    col = AiMessage.__table__.columns.get("routing_feedback")
    assert col is not None, "ai_message.routing_feedback 列必须存在"
    assert col.nullable is True
    assert str(col.type) == "VARCHAR(16)"


def test_ai_message_routing_feedback_check_constraint():
    """CHECK 约束限定 correct、wrong 或 NULL。"""
    from app.modules.ai.models.message import AiMessage

    constraints = {c.name for c in AiMessage.__table__.constraints if c.name}
    assert "ck_ai_message_routing_feedback" in constraints


def test_ai_message_has_nullable_authorization_lineage_columns() -> None:
    expected = {
        "tenant_id",
        "tool_codes",
        "subject_refs",
        "subject_refs_hash",
        "data_scope_hash",
        "resolver_version",
        "projection_dependency_message_ids",
    }

    assert expected <= set(AiMessage.__table__.columns.keys())
    assert all(AiMessage.__table__.columns[name].nullable for name in expected)


def test_message_tombstone_contains_no_business_payload() -> None:
    output = MessageTombstoneOut(
        messageId=9001,
        role="assistant",
    ).model_dump(by_alias=True)

    assert output == {
        "messageId": "9001",
        "role": "assistant",
        "status": "redacted",
        "errorCode": "AI_RESULT_PROJECTION_FORBIDDEN",
    }
