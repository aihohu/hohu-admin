"""Supervisor 路由反馈闭环测试。"""

# ruff: noqa: ARG001, PLC0415  test fixture 占位参数 + 函数内 import（避免顶层副作用）

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_feedback_wrong_recorded(
    client: AsyncClient,
    db_session,
    auth_token,
    mock_visible_agents,
    seed_test_message,
):
    """feedback='wrong' 时更新消息并追加反馈历史。"""
    msg_id = seed_test_message

    response = await client.post(
        f"/ai/messages/{msg_id}/routing-feedback",
        json={"feedback": "wrong", "correctedAgentCode": "dept_mgmt"},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert response.status_code == 200

    from app.modules.ai.models.message import AiMessage

    msg = await db_session.get(AiMessage, msg_id)
    assert msg.routing_feedback == "wrong"

    from sqlalchemy import select

    from app.modules.ai.models.routing_feedback import AiRoutingFeedback

    fb = (
        await db_session.execute(
            select(AiRoutingFeedback).where(AiRoutingFeedback.message_id == msg_id)
        )
    ).scalar_one()
    assert fb.feedback == "wrong"
    assert fb.corrected_agent == "dept_mgmt"
    assert fb.original_agent == "user_mgmt"


@pytest.mark.asyncio
async def test_feedback_wrong_missing_correction_returns_400(
    client: AsyncClient, auth_token, mock_visible_agents, seed_test_message
):
    """错误反馈缺少纠正 Agent 时返回对应错误码。

    注：schema 层 model_validator 会先拦，返回 422；service 层兜底返回 400.
    本测试用缺字段 query，期望 400 OR 422 都可接受（视 FastAPI 默认行为）.
    """
    msg_id = seed_test_message
    response = await client.post(
        f"/ai/messages/{msg_id}/routing-feedback",
        json={"feedback": "wrong"},  # 缺 correctedAgentCode
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert response.status_code in (400, 422)


@pytest.mark.asyncio
async def test_feedback_correction_not_visible_returns_403(
    client: AsyncClient, auth_token, mock_visible_agents, seed_test_message
):
    """correctedAgentCode 不可见时返回 AI_AGENT_NOT_VISIBLE。"""
    msg_id = seed_test_message
    response = await client.post(
        f"/ai/messages/{msg_id}/routing-feedback",
        json={"feedback": "wrong", "correctedAgentCode": "nonexistent_agent"},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert response.status_code == 403
    assert "AI_AGENT_NOT_VISIBLE" in response.text


@pytest.mark.asyncio
async def test_admin_can_feedback_other_users_message(
    client: AsyncClient,
    auth_token,
    mock_visible_agents,
    seed_test_message_other_user,
):
    """消息 owner 或超级管理员可以提交反馈。

    原计划测 not_owner 403，但现有测试体系用 admin token（超管），无法构造
    "非 owner 且非超管"场景——所以反向测：admin 反馈 other_user 消息应 200.
    not_owner 403 场景留给手动测试 / 后续补普通用户 fixture.
    """
    msg_id = seed_test_message_other_user
    response = await client.post(
        f"/ai/messages/{msg_id}/routing-feedback",
        json={"feedback": "wrong", "correctedAgentCode": "dept_mgmt"},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    # 超级管理员可以反馈其他用户的消息。
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_feedback_message_not_found_returns_404(
    client: AsyncClient, auth_token, mock_visible_agents
):
    """消息不存在时返回 AI_MESSAGE_NOT_FOUND。"""
    response = await client.post(
        "/ai/messages/9999999999/routing-feedback",
        json={"feedback": "wrong", "correctedAgentCode": "dept_mgmt"},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_feedback_upsert_overwrites(
    client: AsyncClient,
    db_session,
    auth_token,
    mock_visible_agents,
    seed_test_message,
):
    """重复提交使用 upsert 更新最新纠正结果，并追加历史。"""
    msg_id = seed_test_message

    await client.post(
        f"/ai/messages/{msg_id}/routing-feedback",
        json={"feedback": "wrong", "correctedAgentCode": "dept_mgmt"},
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    await client.post(
        f"/ai/messages/{msg_id}/routing-feedback",
        json={"feedback": "wrong", "correctedAgentCode": "role_mgmt"},
        headers={"Authorization": f"Bearer {auth_token}"},
    )

    from app.modules.ai.models.message import AiMessage

    msg = await db_session.get(AiMessage, msg_id)
    assert msg.routing_feedback == "wrong"

    from sqlalchemy import select

    from app.modules.ai.models.routing_feedback import AiRoutingFeedback

    # PostgreSQL 不保证无 ORDER BY 时的返回顺序，用 order_by feedback_id 比对
    history = (
        (
            await db_session.execute(
                select(AiRoutingFeedback.corrected_agent)
                .where(AiRoutingFeedback.message_id == msg_id)
                .order_by(AiRoutingFeedback.feedback_id)
            )
        )
        .scalars()
        .all()
    )
    assert history == ["dept_mgmt", "role_mgmt"]
