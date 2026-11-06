"""Unadapted writers cannot reach the generic registry callback in production."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.tools.meta import AiToolMeta


async def test_unadapted_writer_is_denied_before_session_or_callback(monkeypatch):
    handler = AsyncMock(side_effect=AssertionError("unplanned writer ran"))
    sessions = Mock(side_effect=AssertionError("ordinary session created"))
    monkeypatch.setattr(executor, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(executor, "record_failure", AsyncMock())
    result = await executor._invoke_tool_fn(
        SimpleNamespace(
            meta=AiToolMeta(
                name="test.unadapted",
                agent="test",
                summary="test",
                required_perms=("test:write",),
                risk="low",
            ),
            fn=handler,
        ),
        {},
        SimpleNamespace(user=SimpleNamespace(user_id=1), tenant=None),
        "a" * 64,
    )
    assert not result.ok and result.error_code == "AI_PREPARED_ACTION_REQUIRED"
    sessions.assert_not_called()
    handler.assert_not_awaited()
