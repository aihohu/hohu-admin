"""Optional notice delivery reports persistence only after a confirmed owner ACK."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import BusinessException
from app.core.tenant import TenantContext
from app.modules.ai.agents.hitl.events import (
    AiErrorEvent,
    DoneEvent,
    ToolCallResultEvent,
)
from app.modules.ai.api import resume


@pytest.mark.parametrize(
    "outcome", ["committed", "none", "failed", "unknown", "cancelled"]
)
async def test_optional_notice_requires_confirmed_owner_ack(monkeypatch, outcome):
    finalize = AsyncMock(return_value=7001 if outcome == "committed" else None)
    if outcome == "failed":
        finalize.side_effect = RuntimeError("Injected owner failure")
    elif outcome == "unknown":
        finalize.side_effect = BusinessException(
            503, "Unknown commit", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )
    elif outcome == "cancelled":
        finalize.side_effect = asyncio.CancelledError()
    runtime = SimpleNamespace(chat=SimpleNamespace(finalize=finalize))
    monkeypatch.setattr(resume, "get_authorization_runtime", lambda: runtime)
    pending = SimpleNamespace(
        trace_id="unknown-trace",
        conversation_id=5001,
        source_user_message_id=6001,
        agent_code="user_mgmt",
    )
    tenant = TenantContext(0, "fixture", 101, 1, "access_token")
    events = await resume._persist_unknown_resume_notice(
        pending, tenant=tenant, actor_user_version=1
    )
    finalize.assert_awaited_once_with(
        conversation_id=5001,
        trace_id="unknown-trace",
        source_id=6001,
        content="",
        tool_calls=[],
        agent_code="user_mgmt",
        tenant=tenant,
        actor_user_version=1,
    )
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert len(done) == 1
    assert done[0].persistence == (
        "committed" if outcome == "committed" else "not_applicable"
    )
    assert done[0].projection == ("updated" if outcome == "committed" else "unchanged")
    assert done[0].message_id == (7001 if outcome == "committed" else None)
    assert [
        event.error_code for event in events if isinstance(event, AiErrorEvent)
    ] == ["AI_EXECUTION_OUTCOME_UNKNOWN"]
    assert not any(isinstance(event, ToolCallResultEvent) for event in events)
