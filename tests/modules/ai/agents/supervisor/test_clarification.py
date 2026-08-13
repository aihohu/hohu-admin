"""clarification_required SSE 事件和无状态协议测试。"""

import dataclasses
import json
import typing

from app.modules.ai.agents.hitl.events import (
    AiStreamEvent,
    ClarificationRequiredEvent,
    event_to_sse_data,
)


def test_clarification_event_serializes():
    """ClarificationRequiredEvent 包含 candidates 和 message。"""
    ev = ClarificationRequiredEvent(
        candidates=(
            {"code": "user_mgmt", "name": "用户管理助手", "description": "..."},
            {"code": "dept_mgmt", "name": "部门管理助手", "description": "..."},
        ),
        message="请问你想查询用户还是部门？",
        reason_code="selection_required",
    )
    payload = json.loads(event_to_sse_data(ev))
    assert payload["type"] == "clarification_required"
    assert len(payload["candidates"]) == 2
    assert payload["candidates"][0]["code"] == "user_mgmt"
    assert payload["message"] == "请问你想查询用户还是部门？"
    assert payload["reasonCode"] == "selection_required"
    # 无状态化：不存 confirmationId / expiresAt
    assert "confirmationId" not in payload
    assert "expiresAt" not in payload


def test_clarification_event_no_state_fields():
    """澄清事件不包含 Redis confirmationId 等状态字段。"""
    field_names = {f.name for f in dataclasses.fields(ClarificationRequiredEvent)}
    assert "confirmation_id" not in field_names
    assert "expires_at" not in field_names


def test_clarification_event_in_ai_stream_event_union():
    """ClarificationRequiredEvent 必须加入 AiStreamEvent Union（被 event_to_sse_data 接受）."""
    union_args = set(typing.get_args(AiStreamEvent))
    assert ClarificationRequiredEvent in union_args
