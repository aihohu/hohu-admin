"""Agent admin service (spec §6.1).

注意：Service 层不 commit，由 API 层 `await db.commit()`。
agent_id 序列化由 AgentAdminListItem/DetailItem 上的 @field_serializer 处理
（返回 str(v)），Service 直接用 model_validate(agent) 走 from_attributes=True，
无需手动构造 dict 或 str() 包裹（Task 1 c5748b2 已统一约定）。
"""

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException, NotFoundException
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

    async def _get_agent_or_404(self, db: AsyncSession, agent_id: int) -> AiAgent:
        """决策 #6: 公共 fetch + raise，被 get_agent / update_agent 复用 (DRY)."""
        agent = await db.get(AiAgent, agent_id)
        if agent is None:
            raise NotFoundException(
                resource_type="AI Agent",
                error_code="AI_AGENT_NOT_FOUND",
            )
        return agent

    async def get_agent(self, db: AsyncSession, agent_id: int) -> AgentAdminDetailItem:
        agent = await self._get_agent_or_404(db, agent_id)
        return AgentAdminDetailItem.model_validate(agent)

    async def update_agent(
        self, db: AsyncSession, agent_id: int, req: AgentAdminUpdateReq
    ) -> AgentAdminDetailItem:
        agent = await self._get_agent_or_404(db, agent_id)
        data = req.model_dump(exclude_unset=True)
        # 显式忽略 code / is_builtin / agent_id 字段（决策 #1）
        # —— 即使客户端绕过 UI 直接 PUT 也兜底，绝不改 identity 字段
        for forbidden in ("code", "is_builtin", "agent_id"):
            data.pop(forbidden, None)

        # 显式 description 长度校验（决策 #20）—— Schema field_validator 也会捕，
        # 但全局 RequestValidationError handler 返 422 + 无 errorCode，无法满足
        # spec「400 + AI_AGENT_DESC_LENGTH_INVALID」契约。Service 层抛
        # BusinessRuleException 才能精确产出 errorCode 给前端 i18n 映射.
        if "description" in data and data["description"] is not None:
            desc = data["description"]
            # 按 Python len() 计 code point（中英文同权重，决策 #20）
            if not (50 <= len(desc) <= 200):
                raise BusinessRuleException(
                    "description 长度必须在 50-200 字之间",
                    error_code="AI_AGENT_DESC_LENGTH_INVALID",
                )

        # 显式 model_preference 格式校验（决策 #25）—— 同上，需要 400 + 精确
        # errorCode 供前端识别，而非 Pydantic 的 422 通用响应.
        if "model_preference" in data and data["model_preference"] is not None:
            pref = data["model_preference"]
            if not re.match(r"^[a-z0-9_-]+:[a-z0-9_-]+$", pref):
                raise BusinessRuleException(
                    "model_preference 必须为 'provider:model' 格式",
                    error_code="AI_AGENT_MODEL_PREF_FORMAT_INVALID",
                )

        for k, v in data.items():
            setattr(agent, k, v)
        await db.flush()
        # refresh 让 onupdate（如 update_time）服务端默认值回写到对象，
        # 否则返回的 AgentAdminDetailItem 会带上旧 update_time.
        await db.refresh(agent)
        return AgentAdminDetailItem.model_validate(agent)


agent_admin_service = AgentAdminService()
