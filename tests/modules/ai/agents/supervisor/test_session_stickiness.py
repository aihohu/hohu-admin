"""spec §11 test_session_stickiness: agentCode 三种语义的决策树."""

from unittest.mock import AsyncMock, patch

import pytest

from app.modules.ai.agents.supervisor.stickiness import (
    resolve_sticky_agent_code,
)


@pytest.mark.asyncio
async def test_explicit_code_overrides_stickiness(db_session):
    """spec §6.1: 显式 code → manual_override，跳过粘滞 + Supervisor."""
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
    """spec §5.3: agentCode="auto" → 强制 Supervisor 重路由."""
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
    """spec §5.3 / §13 决策 3: agentCode=null + 上轮 agent_code 存在 → 粘滞."""
    decision = await resolve_sticky_agent_code(
        db_session,
        user_id=1,
        conversation_id=10,
        agent_code_param=None,
        conv_agent_code="user_mgmt",
        sticky_agent_enabled=True,  # 跳过 DB 查询
    )
    assert decision.agent_code == "user_mgmt"
    assert decision.reason == "session_sticky"
    assert decision.run_supervisor is False


@pytest.mark.asyncio
async def test_null_without_conv_agent_falls_back_to_auto(db_session):
    """spec §5.3: agentCode=null + 新会话 → 等价 auto → 走 Supervisor."""
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
async def test_sticky_agent_disabled_falls_back_to_auto(db_session):
    """spec §11 test_session_stickiness 边界: 粘滞的 Agent 已被禁用 → fallback auto."""
    decision = await resolve_sticky_agent_code(
        db_session,
        user_id=1,
        conversation_id=10,
        agent_code_param=None,
        conv_agent_code="user_mgmt",
        sticky_agent_enabled=False,  # 模拟 Agent 已禁用
    )
    assert decision.run_supervisor is True
    assert decision.reason == "auto_fallback_disabled"


@pytest.mark.asyncio
async def test_legacy_null_mode_uses_default_agent_code(db_session):
    """spec §13 决策 21 / §15.3: routing_legacy_null_mode=True + null → DEFAULT_AGENT_CODE 旧行为."""
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
