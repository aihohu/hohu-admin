"""Agent admin service (spec §6.1).

注意：Service 层不 commit，由 API 层 `await db.commit()`。
agent_id 序列化由 AgentAdminListItem/DetailItem 上的 @field_serializer 处理
（返回 str(v)），Service 直接用 model_validate(agent) 走 from_attributes=True，
无需手动构造 dict 或 str() 包裹（Task 1 c5748b2 已统一约定）。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.schemas.agent_admin import (
    AgentAdminDetailItem,
    AgentAdminListItem,
    AgentAdminUpdateReq,
)


class AgentAdminService:
    async def list_agents(self, db: AsyncSession) -> list[AgentAdminListItem]:
        result = await db.execute(
            select(AiAgent).order_by(AiAgent.display_order, AiAgent.agent_id)
        )
        agents = result.scalars().all()
        return [AgentAdminListItem.model_validate(a) for a in agents]

    async def get_agent(self, db: AsyncSession, agent_id: int) -> AgentAdminDetailItem:
        agent = await db.get(AiAgent, agent_id)
        if agent is None:
            raise NotFoundException(
                resource_type="AI Agent",
                error_code="AI_AGENT_NOT_FOUND",
            )
        return AgentAdminDetailItem.model_validate(agent)

    async def update_agent(
        self, db: AsyncSession, agent_id: int, req: AgentAdminUpdateReq
    ) -> AgentAdminDetailItem:
        agent = await db.get(AiAgent, agent_id)
        if agent is None:
            raise NotFoundException(
                resource_type="AI Agent",
                error_code="AI_AGENT_NOT_FOUND",
            )
        data = req.model_dump(exclude_unset=True)
        # 显式忽略 code / is_builtin / agent_id 字段（决策 #1）
        # —— 即使客户端绕过 UI 直接 PUT 也兜底，绝不改 identity 字段
        for forbidden in ("code", "is_builtin", "agent_id"):
            data.pop(forbidden, None)
        for k, v in data.items():
            setattr(agent, k, v)
        await db.flush()
        return await self.get_agent(db, agent_id)


agent_admin_service = AgentAdminService()
