"""spec §2.2 v1.5+: ConfirmationResumedEvent 序列化测试

断言风格对齐 tests/modules/ai/test_events.py：用 json.loads 解析后检查结构，
不依赖底层 _compact_json 的具体空格策略（robust to separators 调整）。
"""

# ruff: noqa: ARG001, PLC0415

import json

from app.modules.ai.agents.hitl.events import (
    ConfirmationResumedEvent,
    DryRunSummary,
    event_to_sse_data,
)


class TestConfirmationResumedEventSerialization:
    def test_basic_fields_camel_case(self) -> None:
        ev = ConfirmationResumedEvent(
            confirmation_id="abc123",
            tool="user.update_dept",
            tool_call_id="tc_xxx",
            summary="tool=user.update_dept, risk=high",
            args={"user_ids": [1, 2]},
            expires_at="2026-07-13T15:00:00Z",
            resumed_at="2026-07-13T14:35:00Z",
        )
        data = json.loads(event_to_sse_data(ev))
        assert data["type"] == "confirmation_resumed"
        assert data["confirmationId"] == "abc123"
        assert data["toolCallId"] == "tc_xxx"
        assert data["resumedAt"] == "2026-07-13T14:35:00Z"
        assert data["expiresAt"] == "2026-07-13T15:00:00Z"
        # snake_case 原字段不应出现在顶层
        assert "confirmation_id" not in data
        assert "tool_call_id" not in data
        assert "resumed_at" not in data

    def test_dry_run_serialized_when_present(self) -> None:
        ev = ConfirmationResumedEvent(
            confirmation_id="abc",
            tool="user.batch_delete",
            tool_call_id="tc_yyy",
            summary="...",
            args={},
            dry_run=DryRunSummary(summary="将影响 3 行", affected_count=3),
            expires_at="2026-07-13T15:00:00Z",
            resumed_at="2026-07-13T14:35:00Z",
        )
        data = json.loads(event_to_sse_data(ev))
        assert data["dryRun"] == {
            "summary": "将影响 3 行",
            "affectedCount": 3,
        }

    def test_dry_run_omitted_when_none(self) -> None:
        ev = ConfirmationResumedEvent(
            confirmation_id="abc",
            tool="t",
            tool_call_id="tc_z",
            summary="s",
            args={},
            expires_at="...",
            resumed_at="...",
        )
        data = json.loads(event_to_sse_data(ev))
        # _compact_json 递归移除 None 字段：dry_run=None 时整个 key 不出现
        assert "dryRun" not in data
