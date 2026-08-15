"""Agent 可见性兼容入口，实际规则由统一授权服务提供。"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.models.agent import AiAgent
from app.modules.ai.service.agent_authorization_service import (
    agent_authorization_service,
)
from app.modules.system.models.user import User


async def list_visible_agents(db: AsyncSession, current_user: User) -> list[AiAgent]:
    """返回通过全局开关、显式角色绑定和 Tool 可见性校验的 Agent。"""
    return await agent_authorization_service.list_agents(db, current_user)
