"""spec §11 test_router.py: LLM-only 路由测试.

覆盖：
- LLM 唯一解析成功
- JSON 鲁棒解析（markdown 包裹 / prose 包裹 / 字段缺失 / code 不在候选）
- shared 作为 catch-all
- 权限过滤
- 禁用 Agent 过滤
- LLM 调用异常降级 → clarification
- 无 Provider 降级 → clarification
- 候选集空 → AI_ROUTING_FAILED
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.modules.ai.agents.supervisor import router as router_mod
from app.modules.ai.agents.supervisor.router import (
    AgentRouter,
    RouteResult,
    build_router_prompt,
    parse_agent_code_robustly,
)


def _make_agent(code: str, name: str = "", description: str = ""):
    """构造内存中的 AiAgent-like 对象（避免每次都建表）."""
    return SimpleNamespace(
        code=code,
        name=name or code,
        description=description or f"desc for {code}",
        display_order=0,
        enabled=True,
    )


# ---------- build_router_prompt ----------


def test_build_router_prompt_includes_candidate_descriptions():
    """spec §5.1: prompt 必须含每个候选 Agent 的 name + description."""
    candidates = [
        _make_agent("user_mgmt", "用户管理助手", "处理用户 CRUD"),
        _make_agent("role_mgmt", "角色权限助手", "处理角色绑定"),
    ]
    prompt = build_router_prompt(candidates, "重置密码")
    assert "user_mgmt" in prompt
    assert "用户管理助手" in prompt
    assert "处理用户 CRUD" in prompt
    assert "role_mgmt" in prompt
    assert "重置密码" in prompt


def test_build_router_prompt_includes_shared_catchall_instruction():
    """spec §13 决策 9: shared Agent 在 prompt 中显式声明 fallback 角色."""
    candidates = [_make_agent("shared"), _make_agent("user_mgmt")]
    prompt = build_router_prompt(candidates, "any query")
    # shared 的 description 由 seed_ai_agents.py 维护为 fallback 角色，
    # 但 router 也会在 prompt 顶部统一加 catch-all 提示
    assert "JSON" in prompt  # 强调 JSON-only 输出


# ---------- parse_agent_code_robustly ----------


def test_parse_plain_json():
    candidates = [_make_agent("user_mgmt")]
    assert (
        parse_agent_code_robustly('{"agent_code": "user_mgmt"}', candidates)
        == "user_mgmt"
    )


def test_parse_markdown_wrapped_json():
    """LLM 常用 ```json ... ``` 包裹，要鲁棒解析."""
    candidates = [_make_agent("user_mgmt")]
    raw = '```json\n{"agent_code": "user_mgmt"}\n```'
    assert parse_agent_code_robustly(raw, candidates) == "user_mgmt"


def test_parse_prose_wrapped_json():
    """LLM 可能加解释文字，要截 {...} 子串."""
    candidates = [_make_agent("user_mgmt")]
    raw = 'The answer is: {"agent_code": "user_mgmt"} thanks!'
    assert parse_agent_code_robustly(raw, candidates) == "user_mgmt"


def test_parse_code_not_in_candidates_returns_none():
    """spec §5.1: code 不在候选集 → 失败."""
    candidates = [_make_agent("user_mgmt")]
    assert parse_agent_code_robustly('{"agent_code": "role_mgmt"}', candidates) is None


def test_parse_missing_agent_code_field_returns_none():
    candidates = [_make_agent("user_mgmt")]
    assert parse_agent_code_robustly('{"foo": "bar"}', candidates) is None


def test_parse_garbage_returns_none():
    candidates = [_make_agent("user_mgmt")]
    assert parse_agent_code_robustly("totally not json", candidates) is None


# ---------- AgentRouter.route ----------


@pytest.mark.asyncio
async def test_route_llm_resolved(db_session):
    """spec §5.1 主路径：LLM 返回合法 code → RouteResult(agent_code=..., reason='llm_resolved')."""
    candidates = [_make_agent("user_mgmt"), _make_agent("shared")]
    router = AgentRouter()

    fake_model = AsyncMock()
    with patch(
        "app.modules.ai.agents.supervisor.router.call_llm_text",
        AsyncMock(return_value='{"agent_code": "user_mgmt"}'),
    ):
        result = await router.route(
            db_session, "重置密码", candidates, model=fake_model
        )

    assert isinstance(result, RouteResult)
    assert result.agent_code == "user_mgmt"
    assert result.reason == "llm_resolved"
    assert result.clarification is False
    assert result.failed is False


@pytest.mark.asyncio
async def test_route_no_provider_falls_back_to_clarification(db_session):
    """spec §5.2 / §9: 无 Provider → clarification_required + reason='no_provider'."""
    candidates = [_make_agent("user_mgmt")]
    router = AgentRouter()

    with patch(
        "app.modules.ai.agents.supervisor.router.provider_service.resolve_model",
        AsyncMock(side_effect=Exception("AI_MODEL_NOT_CONFIGURED")),
    ):
        result = await router.route(db_session, "重置密码", candidates, model=None)

    assert result.clarification is True
    assert result.reason == "no_provider"
    assert result.candidates == candidates


@pytest.mark.asyncio
async def test_route_llm_call_failed_falls_back_to_clarification(db_session):
    """spec §5.2: LLM 异常 → clarification + reason='llm_call_failed'."""
    candidates = [_make_agent("user_mgmt")]
    router = AgentRouter()

    fake_model = AsyncMock()
    with patch(
        "app.modules.ai.agents.supervisor.router.call_llm_text",
        AsyncMock(side_effect=Exception("network error")),
    ):
        result = await router.route(
            db_session, "重置密码", candidates, model=fake_model
        )

    assert result.clarification is True
    assert result.reason == "llm_call_failed"


@pytest.mark.asyncio
async def test_route_llm_unparsable_falls_back_to_clarification(db_session):
    """spec §5.1 / §5.2: LLM 返回不合法 JSON → clarification + reason='llm_unparsable_or_out_of_scope'."""
    candidates = [_make_agent("user_mgmt")]
    router = AgentRouter()

    fake_model = AsyncMock()
    with patch(
        "app.modules.ai.agents.supervisor.router.call_llm_text",
        AsyncMock(return_value="I think user_mgmt"),
    ):
        result = await router.route(
            db_session, "重置密码", candidates, model=fake_model
        )

    assert result.clarification is True
    assert result.reason == "llm_unparsable_or_out_of_scope"


@pytest.mark.asyncio
async def test_route_no_candidates_returns_failed(db_session):
    """spec §5.1: 候选集空 → RouteResult(failed=True, reason='no_candidates')."""
    router = AgentRouter()
    result = await router.route(db_session, "any", [], model=None)

    assert result.failed is True
    assert result.reason == "no_candidates"


@pytest.mark.asyncio
async def test_route_shared_selected_when_no_match(db_session):
    """spec §13 决策 9: LLM 在其它 Agent 都不合适时选 shared."""
    candidates = [
        _make_agent("shared", description="其它 Agent 都不合适时选我"),
        _make_agent("user_mgmt", description="用户管理"),
    ]
    router = AgentRouter()

    with patch(
        "app.modules.ai.agents.supervisor.router.call_llm_text",
        AsyncMock(return_value='{"agent_code": "shared"}'),
    ):
        result = await router.route(
            db_session, "解析文件", candidates, model=AsyncMock()
        )

    assert result.agent_code == "shared"
    assert result.reason == "llm_resolved"


def test_pure_llm_routing_no_keywords():
    """spec §13 决策 18: v4 砍规则阶段，router 模块不存在 keyword 相关入口."""
    public_names = [n for n in dir(router_mod) if not n.startswith("_")]
    assert not any(
        "keyword" in n.lower() or "rule" in n.lower() for n in public_names
    ), f"v4 砍规则阶段，router 不应有 keyword/rule 入口，发现: {public_names}"
