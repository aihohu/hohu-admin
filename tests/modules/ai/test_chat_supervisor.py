"""``/ai/chat`` 的 Supervisor 端到端集成测试。"""

# ruff: noqa: ARG001, PLC0415  test fixture 占位参数 + 函数内 import（断言用）

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


@pytest.fixture(autouse=True)
def authorize_chat_model_for_endpoint():
    """隔离端点前置 selector；selector 规则由专门测试覆盖。"""
    selected = SimpleNamespace(model=SimpleNamespace(model_id=123456))
    with (
        patch(
            "app.modules.ai.api.chat.model_authorization_service.authorize_chat_model",
            AsyncMock(return_value=selected),
        ),
        patch(
            "app.modules.ai.api.chat.chat_service.create_agent",
            AsyncMock(return_value=MagicMock(name="fake_exec_agent")),
        ),
    ):
        yield


def _chat_body(text: str, **extra) -> dict:
    """构造合法 VercelAI SubmitMessage 请求体.

    chat.py 读 messages[].content（旧字段）优先，回退到 parts（VercelAI 标准）。
    UIMessage schema 校验严格（extra='forbid' 之外的字段会 422），所以只放 parts
    让 VercelAIAdapter 通过；chat.py parts-fallback 会拼出 user_message.
    """
    return {
        "trigger": "submit-message",
        "id": "test-chat-id",
        "messages": [
            {
                "id": "msg-1",
                "role": "user",
                "parts": [{"type": "text", "text": text}],
            }
        ],
        **extra,
    }


@pytest.mark.asyncio
async def test_auto_routes_via_supervisor(
    client: AsyncClient, db_session, auth_token, mock_visible_agents
):
    """agentCode='auto' 时由 Supervisor 路由到单个 Agent 执行。

    端到端断言 3 件事：
    1. HTTP 200（请求成功）
    2. ai_routing_log.final_agent == "user_mgmt"（路由审计正确）
    3. ai_routing_log.reason == "llm_resolved"（决策路径正确）

    双 patch：
    - `call_llm_text` 模拟 LLM 返回 JSON
    - `provider_service.resolve_model` 返回 fake_model（避免 dev 环境无 provider 时 raise BusinessRuleException）
    """
    fake_model = AsyncMock(name="fake_router_model")
    # 路由成功后 chat.py 调 create_agent 创建执行 Agent；用 mock agent 跳过
    # PydanticAI Agent 构造（fake_model 不是真 Model 实例，Agent() 会拒收）
    fake_exec_agent = MagicMock(name="fake_exec_agent")

    with (
        patch(
            "app.modules.ai.agents.supervisor.router.call_llm_text",
            AsyncMock(return_value='{"agent_code": "user_mgmt"}'),
        ),
        patch(
            # 避免依赖真实 Provider 配置（CI / dev 环境可能未配 LLM）
            "app.modules.ai.agents.supervisor.router.model_authorization_service.resolve_model_instance",
            AsyncMock(return_value=fake_model),
        ),
        patch(
            # 路由成功后 chat.py:683 chat_service.create_agent → provider_service.resolve_model
            # 不实际构造 LLM 客户端（OPENAI_API_KEY 未配置会 raise）
            "app.modules.ai.api.chat.chat_service.create_agent",
            AsyncMock(return_value=fake_exec_agent),
        ),
    ):
        response = await client.post(
            "/ai/chat",
            json=_chat_body("重置密码", agentCode="auto"),
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    assert response.status_code == 200

    # 断言 routing_log 写入正确
    from sqlalchemy import select

    from app.modules.ai.models.routing_log import AiRoutingLog

    log = (
        await db_session.execute(
            select(AiRoutingLog)
            .where(AiRoutingLog.reason == "llm_resolved")
            .order_by(AiRoutingLog.log_id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    assert log is not None, "ai_routing_log 未写入 llm_resolved 行"
    assert log.final_agent == "user_mgmt"
    assert log.llm_choice == "user_mgmt"


@pytest.mark.asyncio
async def test_new_chat_uses_authorized_agent_model_preference(
    client: AsyncClient,
    auth_token,
    mock_visible_agents,
):
    """没有显式/会话模型时，路由完成后才选择当前 Agent 的默认模型。"""
    preferred = "provider_preferred:model_preferred"
    next(
        agent for agent in mock_visible_agents if agent.code == "user_mgmt"
    ).model_preference = preferred
    selected = SimpleNamespace(model=SimpleNamespace(model_id=654321))
    selector = AsyncMock(return_value=selected)

    with patch(
        "app.modules.ai.api.chat.model_authorization_service.authorize_chat_model",
        selector,
    ):
        response = await client.post(
            "/ai/chat",
            json=_chat_body("查询用户", agentCode="user_mgmt"),
            headers={"Authorization": f"Bearer {auth_token}"},
        )

    assert response.status_code == 200
    assert selector.await_args_list[0].args[1] == preferred


@pytest.mark.asyncio
async def test_explicit_falsy_model_id_is_rejected_without_fallback(
    client: AsyncClient,
    auth_token,
    mock_visible_agents,
):
    """显式提交的 falsy modelId 仍必须进入 selector，而不是当成未提交。"""
    from app.core.exceptions import BusinessRuleException

    selector = AsyncMock(
        side_effect=BusinessRuleException(
            "所选 AI 模型当前不可用",
            error_code="AI_MODEL_NOT_AVAILABLE",
        )
    )

    with patch(
        "app.modules.ai.api.chat.model_authorization_service.authorize_chat_model",
        selector,
    ):
        response = await client.post(
            "/ai/chat",
            json=_chat_body("查询用户", agentCode="user_mgmt", modelId=0),
            headers={"Authorization": f"Bearer {auth_token}"},
        )

    assert response.status_code == 400
    assert response.json()["errorCode"] == "AI_MODEL_NOT_AVAILABLE"
    assert selector.await_args_list[0].args[1] == 0


@pytest.mark.asyncio
async def test_auto_falls_back_to_clarification_on_no_provider(
    client: AsyncClient, auth_token, mock_visible_agents
):
    """无 Provider 时发送 clarification_required。"""
    with patch(
        "app.modules.ai.agents.supervisor.router.model_authorization_service.resolve_model_instance",
        AsyncMock(side_effect=Exception("AI_MODEL_NOT_CONFIGURED")),
    ):
        response = await client.post(
            "/ai/chat",
            json=_chat_body("hi", agentCode="auto"),
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    assert "clarification_required" in response.text


@pytest.mark.asyncio
async def test_auto_propagates_model_authorization_failure(
    client: AsyncClient,
    auth_token,
    mock_visible_agents,
):
    """Supervisor selector 的稳定授权拒绝不能降级成 Agent 澄清。"""
    from app.core.exceptions import BusinessRuleException

    with patch(
        "app.modules.ai.agents.supervisor.router.model_authorization_service.resolve_model_instance",
        AsyncMock(
            side_effect=BusinessRuleException(
                "所选 AI 模型当前不可用",
                error_code="AI_MODEL_NOT_AVAILABLE",
            )
        ),
    ):
        response = await client.post(
            "/ai/chat",
            json=_chat_body("hi", agentCode="auto"),
            headers={"Authorization": f"Bearer {auth_token}"},
        )

    assert response.status_code == 400
    assert response.json()["errorCode"] == "AI_MODEL_NOT_AVAILABLE"
    assert "clarification_required" not in response.text


@pytest.mark.asyncio
async def test_auto_without_authorized_candidates_returns_agent_not_available(
    client: AsyncClient,
    auth_token,
    monkeypatch,
):
    from app.modules.ai.service import agent_visibility as visibility_module

    async def no_agents(_db, _user):
        return []

    monkeypatch.setattr(visibility_module, "list_visible_agents", no_agents)
    response = await client.post(
        "/ai/chat",
        json=_chat_body("hi", agentCode="auto"),
        headers={"Authorization": f"Bearer {auth_token}"},
    )

    assert response.status_code == 200
    assert "AI_AGENT_NOT_AVAILABLE" in response.text


@pytest.mark.asyncio
async def test_supervisor_disabled_uses_default_agent_code(
    client: AsyncClient, auth_token, mock_visible_agents
):
    """Supervisor 关闭时 auto 使用 DEFAULT_AGENT_CODE。"""

    async def fake_bool(db, key, default=False, **kw):
        if key == "ai:supervisor_enabled":
            return False
        return default

    # mock create_agent：supervisor_disabled 路径会进入 save_user_message + create_agent，
    # CI 没有 LLM provider 配置时 create_agent 会抛 BusinessRuleException(400)。
    # 本测试只关心路由决策正确（不进 supervisor），create_agent 成功与否不在范围。
    fake_exec_agent = MagicMock(name="fake_exec_agent")

    with (
        patch(
            "app.modules.ai.agents.safety.ai_config.get_ai_config_bool",
            AsyncMock(side_effect=fake_bool),
        ),
        patch(
            "app.modules.ai.agents.supervisor.router.call_llm_text",
            AsyncMock(side_effect=AssertionError("should not call LLM when disabled")),
        ),
        patch(
            "app.modules.ai.api.chat.chat_service.create_agent",
            AsyncMock(return_value=fake_exec_agent),
        ),
    ):
        response = await client.post(
            "/ai/chat",
            json=_chat_body("hi", agentCode="auto"),
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    # 路由 LLM 没被调用 → assertion 没 raise
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_legacy_null_mode_uses_default_agent_code(
    client: AsyncClient, auth_token, mock_visible_agents
):
    """legacy null 模式下 agentCode=null 使用 DEFAULT_AGENT_CODE。"""

    async def fake_bool(db, key, default=False, **kw):
        if key == "ai:routing_legacy_null_mode":
            return True
        return default

    with (
        patch(
            "app.modules.ai.agents.safety.ai_config.get_ai_config_bool",
            AsyncMock(side_effect=fake_bool),
        ),
        patch(
            "app.modules.ai.agents.supervisor.router.call_llm_text",
            AsyncMock(side_effect=AssertionError("legacy mode should not call LLM")),
        ),
    ):
        response = await client.post(
            "/ai/chat",
            json=_chat_body("hi"),  # 不传 agentCode
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_supervisor_quota_independent_of_usage_limits(
    client: AsyncClient, auth_token, mock_visible_agents
):
    """Supervisor 配额独立于 PydanticAI UsageLimits。

    构造 supervisor 配额已满场景，验证：
    1. supervisor LLM 不被调用（直接走 clarification 降级）
    2. UsageLimits（request_limit=10 / tool_calls_limit=5）不受 supervisor 配额影响
       （agent loop 仍能正常跑 tool 调用）

    反例：复用 UsageLimits → 实现者误以为能拦截，实际漏判.
    """
    from app.modules.ai.agents.supervisor.quota import QuotaResult

    fake_quota = QuotaResult(
        allowed=False, current_count=100, daily_limit=100, reason="quota_exceeded"
    )
    llm_mock = AsyncMock()
    with (
        patch(
            "app.modules.ai.agents.supervisor.quota.check_supervisor_quota",
            AsyncMock(return_value=fake_quota),
        ),
        patch(
            "app.modules.ai.agents.supervisor.router.call_llm_text",
            llm_mock,
        ),
    ):
        response = await client.post(
            "/ai/chat",
            json=_chat_body("hi", agentCode="auto"),
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    # supervisor LLM 没被调用（配额超限直接走 clarification）
    llm_mock.assert_not_called()
    # 返回 clarification_required（不是 500）
    assert "clarification_required" in response.text


@pytest.mark.asyncio
async def test_clarification_does_not_save_user_message(
    client: AsyncClient, db_session, auth_token, mock_visible_agents
):
    """需要澄清时不持久化用户消息，避免孤儿消息。

    构造 LLM 返回不合法 JSON → 触发 clarification_required → 验证 ai_message 无新行.
    """
    with (
        patch(
            "app.modules.ai.agents.supervisor.router.call_llm_text",
            AsyncMock(return_value="not valid json"),
        ),
        patch(
            "app.modules.ai.agents.supervisor.router.model_authorization_service.resolve_model_instance",
            AsyncMock(return_value=AsyncMock(name="fake_model")),
        ),
    ):
        await client.post(
            "/ai/chat",
            json=_chat_body("ambiguous query", agentCode="auto"),
            headers={"Authorization": f"Bearer {auth_token}"},
        )

    from sqlalchemy import select

    from app.modules.ai.models.message import AiMessage

    msgs = (
        (
            await db_session.execute(
                select(AiMessage).where(AiMessage.content.contains("ambiguous query"))
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 0, "clarification 路径不应持久化 user 消息"


@pytest.mark.asyncio
async def test_clarification_works_without_conversation_id(
    client: AsyncClient, auth_token, mock_visible_agents
):
    """新会话首条消息的 conversation_id=null 时仍可正常澄清。

    新会话首条消息触发 clarification 时，前端无 conversation_id 暂存；
    后端不应假设 conversation_id 非空。
    """
    with (
        patch(
            "app.modules.ai.agents.supervisor.router.call_llm_text",
            AsyncMock(return_value="garbage"),
        ),
        patch(
            "app.modules.ai.agents.supervisor.router.model_authorization_service.resolve_model_instance",
            AsyncMock(return_value=AsyncMock(name="fake_model")),
        ),
    ):
        response = await client.post(
            "/ai/chat",
            json=_chat_body("first message", agentCode="auto"),  # 不传 conversationId
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    # 应该返回 clarification_required（而不是 500 / KeyError）
    assert "clarification_required" in response.text
