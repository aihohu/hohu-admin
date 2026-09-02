"""Agent admin service。

注意：Service 层不 commit，由 API 层 `await db.commit()`。
agent_id 序列化由 AgentAdminListItem/DetailItem 上的 @field_serializer 处理
（返回 str(v)），Service 直接用 model_validate(agent) 走 from_attributes=True，
无需手动构造 dict 或用 str() 包裹 ID。
"""

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BusinessRuleException,
    NotFoundException,
)
from app.core.tenant import PlatformContext
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.schemas.agent_admin import (
    AgentAdminDetailItem,
    AgentAdminListItem,
    AgentAdminUpdateReq,
)


def _require_platform(platform: PlatformContext) -> None:
    if not isinstance(platform, PlatformContext):
        raise TypeError("platform context is required")


class AgentAdminService:
    async def list_agents(
        self, db: AsyncSession, *, platform: PlatformContext
    ) -> list[AgentAdminListItem]:
        _require_platform(platform)
        result = await db.execute(
            select(AiAgent).order_by(AiAgent.display_order, AiAgent.agent_id)
        )
        agents = result.scalars().all()
        return [AgentAdminListItem.model_validate(a) for a in agents]

    async def _get_agent_or_404(
        self, db: AsyncSession, agent_id: int, *, platform: PlatformContext
    ) -> AiAgent:
        """决策 #6: 公共 fetch + raise，被 get_agent / update_agent 复用 (DRY)."""
        _require_platform(platform)
        agent = await db.get(AiAgent, agent_id)
        if agent is None:
            raise NotFoundException(
                resource_type="AI Agent",
                error_code="AI_AGENT_NOT_FOUND",
            )
        return agent

    async def get_agent(
        self, db: AsyncSession, agent_id: int, *, platform: PlatformContext
    ) -> AgentAdminDetailItem:
        _require_platform(platform)
        agent = await self._get_agent_or_404(db, agent_id, platform=platform)
        return AgentAdminDetailItem.model_validate(agent)

    async def update_agent(
        self,
        db: AsyncSession,
        agent_id: int,
        req: AgentAdminUpdateReq,
        *,
        platform: PlatformContext,
    ) -> AgentAdminDetailItem:
        _require_platform(platform)

        immutable_fields = {"agent_id", "code", "is_builtin"}
        if immutable_fields & req.model_fields_set:
            raise BusinessRuleException(
                "Agent identity 字段不可修改",
                error_code="AI_AGENT_IMMUTABLE_FIELD",
            )

        agent = await self._get_agent_or_404(db, agent_id, platform=platform)
        data = req.model_dump(exclude_unset=True)

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

        # Accept the stable modelId emitted by the safe option endpoint while
        # retaining the legacy provider:model reference during rolling upgrades.
        if "model_preference" in data and data["model_preference"] is not None:
            pref = data["model_preference"]
            if not re.fullmatch(
                r"(?:[1-9][0-9]*|[a-z0-9_-]+:[a-z0-9_-]+)",
                pref,
            ):
                raise BusinessRuleException(
                    "model_preference 必须为 modelId 或 'provider:model' 格式",
                    error_code="AI_AGENT_MODEL_PREFERENCE_INVALID",
                )

        # 显式校验 daily_quota_per_user 取值；Pydantic
        # field_validator 会返 422 + 无 errorCode，无法满足「400 + AI_AGENT_QUOTA_INVALID」
        # 契约，故移到 Service 层抛 BusinessRuleException.
        if "daily_quota_per_user" in data and data["daily_quota_per_user"] is not None:
            quota = data["daily_quota_per_user"]
            if quota <= 0:
                raise BusinessRuleException(
                    "daily_quota_per_user 必须 ≥ 1 或 null",
                    error_code="AI_AGENT_QUOTA_INVALID",
                )

        for k, v in data.items():
            setattr(agent, k, v)
        await db.flush()
        # refresh 让 onupdate（如 update_time）服务端默认值回写到对象，
        # 否则返回的 AgentAdminDetailItem 会带上旧 update_time.
        await db.refresh(agent)
        return AgentAdminDetailItem.model_validate(agent)


agent_admin_service = AgentAdminService()
