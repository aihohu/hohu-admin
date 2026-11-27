from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from app.core.exceptions import AuthorizationException
from app.modules.ai.agents import chat_agent


async def test_revoked_context_never_reaches_provider(monkeypatch):
    calls = []

    def provider(_messages, _info):
        calls.append(True)
        return ModelResponse(parts=[TextPart("answer")])

    monkeypatch.setattr(chat_agent, "build_pydantic_ai_tools", lambda *_a, **_k: [])
    monkeypatch.setattr(chat_agent, "build_system_prompt", lambda *_a, **_k: "safe")
    check = AsyncMock(
        side_effect=AuthorizationException(error_code="FIELD_AUTHORIZATION_CHANGED")
    )
    agent = chat_agent.create_chat_agent(
        FunctionModel(provider), user_perms=set(), agent_code="shared"
    )
    with pytest.raises(AuthorizationException):
        await agent.run(
            "hello", deps=SimpleNamespace(authorize_model_context=check, agent=None)
        )
    check.assert_awaited_once()
    assert not calls


async def test_context_is_rechecked_before_followup_model_request(monkeypatch):
    calls, tools = [], []

    async def lookup():
        tools.append(True)
        return "authorized result"

    def provider(_messages, _info):
        calls.append(True)
        return ModelResponse(parts=[ToolCallPart("lookup", {}, "lookup-1")])

    monkeypatch.setattr(
        chat_agent, "build_pydantic_ai_tools", lambda *_a, **_k: [Tool(lookup)]
    )
    monkeypatch.setattr(chat_agent, "build_system_prompt", lambda *_a, **_k: "safe")
    check = AsyncMock(
        side_effect=[
            None,
            AuthorizationException(error_code="FIELD_AUTHORIZATION_CHANGED"),
        ]
    )
    agent = chat_agent.create_chat_agent(
        FunctionModel(provider), user_perms=set(), agent_code="shared"
    )
    with pytest.raises(AuthorizationException):
        await agent.run(
            "hello", deps=SimpleNamespace(authorize_model_context=check, agent=None)
        )
    assert check.await_count == 2
    assert calls == [True]
    assert tools == [True]
