"""agentCode 三种语义的粘滞路由决策树测试。"""

from unittest.mock import AsyncMock, patch

import pytest

from app.modules.ai.agents.supervisor.stickiness import (
    resolve_sticky_agent_code,
)


@pytest.mark.asyncio
async def test_explicit_code_overrides_stickiness(db_session):
    """显式 code 使用 manual_override，跳过粘滞和 Supervisor。"""
    decision = await resolve_sticky_agent_code(
        db_session,
        user_id=1,
        conversation_id=10,
        agent_code_param="user_mgmt",
        conv_agent_code="role_mgmt",  # 即使会话上轮是 role_mgmt
    )
    assert decision.agent_code == "user_mgmt"
    assert decision.reason == "manual_override"
    assert decision.run_supervisor is False


@pytest.mark.asyncio
async def test_auto_forces_supervisor(db_session):
    """agentCode='auto' 强制 Supervisor 重新路由。"""
    decision = await resolve_sticky_agent_code(
        db_session,
        user_id=1,
        conversation_id=10,
        agent_code_param="auto",
        conv_agent_code="user_mgmt",  # 即使会话上轮是 user_mgmt
    )
    assert decision.run_supervisor is True
    assert decision.reason == "auto_explicit"


@pytest.mark.asyncio
async def test_null_reuses_last_agent(db_session):
    """agentCode=null 且存在上轮 agent_code 时保持粘滞。"""
    decision = await resolve_sticky_agent_code(
        db_session,
        user_id=1,
        conversation_id=10,
        agent_code_param=None,
        conv_agent_code="user_mgmt",
    )
    assert decision.agent_code == "user_mgmt"
    assert decision.reason == "session_sticky"
    assert decision.run_supervisor is False


@pytest.mark.asyncio
async def test_null_without_conv_agent_falls_back_to_auto(db_session):
    """新会话中 agentCode=null 等价于 auto。"""
    decision = await resolve_sticky_agent_code(
        db_session,
        user_id=1,
        conversation_id=None,  # 新会话
        agent_code_param=None,
        conv_agent_code=None,
    )
    assert decision.run_supervisor is True
    assert decision.reason == "auto_fallback"


@pytest.mark.asyncio
async def test_sticky_value_is_deferred_to_unified_agent_policy(db_session):
    """stickiness 只分类；禁用、解绑与 Tool 可见性统一由 Agent Policy 复核。"""
    decision = await resolve_sticky_agent_code(
        db_session,
        user_id=1,
        conversation_id=10,
        agent_code_param=None,
        conv_agent_code="user_mgmt",
    )
    assert decision.run_supervisor is False
    assert decision.agent_code == "user_mgmt"
    assert decision.reason == "session_sticky"


@pytest.mark.asyncio
async def test_legacy_null_mode_uses_default_agent_code(db_session):
    """legacy null 模式下 null 使用 DEFAULT_AGENT_CODE。"""
    with patch(
        "app.modules.ai.agents.supervisor.stickiness.get_ai_config_bool",
        AsyncMock(return_value=True),
    ):
        decision = await resolve_sticky_agent_code(
            db_session,
            user_id=1,
            conversation_id=10,
            agent_code_param=None,
            conv_agent_code="user_mgmt",  # 即使有粘滞值也忽略
        )
    assert decision.agent_code == "user_mgmt"  # DEFAULT_AGENT_CODE
    assert decision.reason == "legacy_null_mode"
    assert decision.run_supervisor is False
