"""spec §3.2 v1.5+: confirmation_required SSE 帧自动附 id: 字段（当 AI_SSE_RESUME_ENABLED=True）"""

# ruff: noqa: ARG001, PLC0415

from unittest.mock import patch

import pytest

from app.modules.ai.agents.hitl.events import (
    ConfirmationRequiredEvent,
    DoneEvent,
    ToolCallStartedEvent,
)
from app.modules.ai.api.chat import _format_sse_chunk


@pytest.fixture
def _resume_enabled():
    with patch("app.modules.ai.api.chat.settings.AI_SSE_RESUME_ENABLED", True):
        yield


@pytest.fixture
def _resume_disabled():
    with patch("app.modules.ai.api.chat.settings.AI_SSE_RESUME_ENABLED", False):
        yield


class TestFormatSseChunkIdField:
    def test_confirmation_required_has_id_when_enabled(self, _resume_enabled) -> None:
        ev = ConfirmationRequiredEvent(
            confirmation_id="cid_abc",
            tool="t",
            tool_call_id="tc_x",
            summary="s",
            args={},
            expires_at="...",
        )
        chunk = _format_sse_chunk(ev)
        assert "id: cid_abc\n" in chunk
        assert chunk.endswith("\n\n")

    def test_confirmation_required_no_id_when_disabled(self, _resume_disabled) -> None:
        ev = ConfirmationRequiredEvent(
            confirmation_id="cid_abc",
            tool="t",
            tool_call_id="tc_x",
            summary="s",
            args={},
            expires_at="...",
        )
        chunk = _format_sse_chunk(ev)
        assert "id:" not in chunk

    def test_other_events_have_no_id(self, _resume_enabled) -> None:
        """只有 confirmation_required 应带 id:（其它事件 sequence_id 无意义）"""
        ev = ToolCallStartedEvent(
            tool="t",
            tool_call_id="tc_x",
            summary="s",
            args={},
            risk="low",
            trace_id="tr_x",
        )
        chunk = _format_sse_chunk(ev)
        assert "id:" not in chunk

    def test_done_event_no_id(self, _resume_enabled) -> None:
        chunk = _format_sse_chunk(DoneEvent())
        assert "id:" not in chunk
