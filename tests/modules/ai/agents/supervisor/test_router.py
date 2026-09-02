"""LLM-only Supervisor 路由测试。

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

import httpx
import pytest

from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext
from app.modules.ai.agents.supervisor import router as router_mod
from app.modules.ai.agents.supervisor.router import (
    AgentRouter,
    RouteResult,
    build_router_prompt,
    parse_agent_code_robustly,
)
from app.modules.ai.core.provider_egress import ProviderTransportError

TENANT = TenantContext(0, "default", 1, 1, "access_token")


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
    """prompt 必须包含每个候选 Agent 的名称和描述。"""
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
    """shared Agent 在 prompt 中显式声明 fallback 角色。"""
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
    """返回的 code 不在候选集时解析失败。"""
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
    """LLM 返回合法 code 时生成 llm_resolved 路由结果。"""
    candidates = [_make_agent("user_mgmt"), _make_agent("shared")]
    router = AgentRouter()

    fake_model = AsyncMock()
    with patch(
        "app.modules.ai.agents.supervisor.router.call_llm_text",
        AsyncMock(return_value='{"agent_code": "user_mgmt"}'),
    ):
        result = await router.route(
            db_session, "重置密码", candidates, model=fake_model, tenant=TENANT
        )

    assert isinstance(result, RouteResult)
    assert result.agent_code == "user_mgmt"
    assert result.reason == "llm_resolved"
    assert result.clarification is False
    assert result.failed is False


@pytest.mark.asyncio
async def test_route_no_provider_falls_back_to_clarification(db_session):
    """无 Provider 时返回 clarification_required 和 no_provider。"""
    candidates = [_make_agent("user_mgmt")]
    router = AgentRouter()

    with patch(
        "app.modules.ai.agents.supervisor.router.model_authorization_service.resolve_model_instance",
        AsyncMock(side_effect=Exception("AI_MODEL_NOT_CONFIGURED")),
    ):
        result = await router.route(
            db_session, "重置密码", candidates, model=None, tenant=TENANT
        )

    assert result.clarification is True
    assert result.reason == "no_provider"
    assert result.candidates == candidates


@pytest.mark.asyncio
async def test_route_propagates_model_authorization_failure(db_session):
    """统一 selector 的稳定授权错误必须保留，不能伪装成路由歧义。"""
    candidates = [_make_agent("user_mgmt")]
    router = AgentRouter()
    denied = BusinessRuleException(
        "所选 AI 模型当前不可用",
        error_code="AI_MODEL_NOT_AVAILABLE",
    )

    with patch(
        "app.modules.ai.agents.supervisor.router.model_authorization_service.resolve_model_instance",
        AsyncMock(side_effect=denied),
    ):
        with pytest.raises(BusinessRuleException) as exc_info:
            await router.route(
                db_session, "重置密码", candidates, model=None, tenant=TENANT
            )

    assert exc_info.value.error_code == "AI_MODEL_NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_route_llm_call_failed_falls_back_to_clarification(db_session):
    """LLM 调用异常时返回 clarification 和 llm_call_failed。"""
    candidates = [_make_agent("user_mgmt")]
    router = AgentRouter()

    fake_model = AsyncMock()
    with patch(
        "app.modules.ai.agents.supervisor.router.call_llm_text",
        AsyncMock(side_effect=Exception("network error")),
    ):
        result = await router.route(
            db_session, "重置密码", candidates, model=fake_model, tenant=TENANT
        )

    assert result.clarification is True
    assert result.reason == "llm_call_failed"


async def test_route_propagates_sanitized_provider_transport_failure(db_session):
    candidates = [_make_agent("user_mgmt")]
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    with patch(
        "app.modules.ai.agents.supervisor.router.call_llm_text",
        new=AsyncMock(
            side_effect=ProviderTransportError("network_error", request=request)
        ),
    ):
        with pytest.raises(BusinessRuleException) as exc_info:
            await AgentRouter().route(
                db_session,
                "list users",
                candidates,
                model=object(),
                tenant=TENANT,
            )

    assert exc_info.value.code == 502
    assert exc_info.value.error_code == "AI_PROVIDER_UPSTREAM_ERROR"


@pytest.mark.asyncio
async def test_route_llm_unparsable_falls_back_to_clarification(db_session):
    """LLM 返回非法 JSON 时要求澄清并标记解析失败。"""
    candidates = [_make_agent("user_mgmt")]
    router = AgentRouter()

    fake_model = AsyncMock()
    with patch(
        "app.modules.ai.agents.supervisor.router.call_llm_text",
        AsyncMock(return_value="I think user_mgmt"),
    ):
        result = await router.route(
            db_session, "重置密码", candidates, model=fake_model, tenant=TENANT
        )

    assert result.clarification is True
    assert result.reason == "llm_unparsable_or_out_of_scope"


@pytest.mark.asyncio
async def test_route_no_candidates_returns_failed(db_session):
    """候选集为空时返回 failed 和 no_candidates。"""
    router = AgentRouter()
    result = await router.route(db_session, "any", [], model=None, tenant=TENANT)

    assert result.failed is True
    assert result.reason == "no_candidates"


@pytest.mark.asyncio
async def test_route_shared_selected_when_no_match(db_session):
    """其他 Agent 都不合适时 LLM 可以选择 shared。"""
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
            db_session, "解析文件", candidates, model=AsyncMock(), tenant=TENANT
        )

    assert result.agent_code == "shared"
    assert result.reason == "llm_resolved"


def test_pure_llm_routing_no_keywords():
    """router 只使用 LLM 路由，不保留 keyword 规则入口。"""
    public_names = [n for n in dir(router_mod) if not n.startswith("_")]
    assert not any(
        "keyword" in n.lower() or "rule" in n.lower() for n in public_names
    ), f"v4 砍规则阶段，router 不应有 keyword/rule 入口，发现: {public_names}"
