"""Role-Agent binding service (spec §6.3).

职责：
- GET：返回 allAgents + boundAgentIds，不暴露软禁用段（决策 #19）
- PUT：全量覆盖（DELETE + INSERT），normalize 软禁用态为 enabled=True（决策 #15）

跨模块校验 role 存在抛 AI 前缀 errorCode（决策 #18），方便前端 i18n 区分模块归属.
Service 不 commit，由 API 层 `await db.commit()`（CLAUDE.md 硬规则 #9）.

put_binding 在 Task 6 内 ship 但 Task 7 才补 PUT 边界测试 —— 提前实现避免后续
大段重写 service，TDD 红绿针对 GET 即可.
"""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException, NotFoundException
from app.modules.ai.agents.tools.meta import SHARED_AGENT_CODE
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.schemas.role_agent import (
    AgentRow,
    RoleAgentBinding,
    RoleAgentBindReq,
)
from app.modules.system.models.role import Role


class RoleAgentService:
    async def _get_role_or_404(self, db: AsyncSession, role_id: int) -> Role:
        """跨模块校验 role 存在 —— 抛 AI 前缀 errorCode（决策 #18）.

        与 agent_admin._get_agent_or_404 同构（决策 #6 公共 fetch + raise）.
        """
        role = await db.get(Role, role_id)
        if role is None:
            raise NotFoundException(
                resource_type="Role", error_code="AI_ROLE_NOT_FOUND"
            )
        return role

    async def get_binding(self, db: AsyncSession, role_id: int) -> RoleAgentBinding:
        """GET：返回 allAgents + boundAgentIds.

        - allAgents: 全量 Agent（含禁用、含 shared），按 displayOrder + agentId 排序.
        - boundAgentIds: 该 role 当前 enabled=True 的绑定 agent_id 列表.
        - 不返回 softDisabledAgentIds 段（决策 #19）.
        """
        await self._get_role_or_404(db, role_id)

        agents = (
            (
                await db.execute(
                    select(AiAgent).order_by(AiAgent.display_order, AiAgent.agent_id)
                )
            )
            .scalars()
            .all()
        )

        bound_rows = (
            (
                await db.execute(
                    select(RoleAiAgent.agent_id).where(
                        RoleAiAgent.role_id == role_id,
                        RoleAiAgent.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )

        return RoleAgentBinding(
            role_id=role_id,
            all_agents=[
                AgentRow(
                    agent_id=a.agent_id,
                    code=a.code,
                    name=a.name,
                    description=a.description,
                    enabled=a.enabled,
                    is_builtin=a.is_builtin,
                    is_shared=(a.code == SHARED_AGENT_CODE),
                )
                for a in agents
            ],
            bound_agent_ids=[str(aid) for aid in bound_rows],
        )

    async def put_binding(
        self, db: AsyncSession, role_id: int, req: RoleAgentBindReq
    ) -> None:
        """PUT：全量覆盖（决策 #15）—— DELETE 旧绑定 + INSERT 新绑定.

        - 跨模块校验 role 存在（AI_ROLE_NOT_FOUND）
        - agent_ids 去重
        - 每个 agent_id 校验存在（AI_AGENT_NOT_FOUND）
        - shared Agent 拦截（AI_ROLE_AGENT_BIND_SHARED_FORBIDDEN，决策 #14）
        - normalize：所有新绑定 enabled=True（软禁用态归零，决策 #15 全量覆盖语义）

        Task 7 补 PUT 边界测试（shared 拦截 / agent 不存在 / 全量覆盖 normalize）.
        """
        await self._get_role_or_404(db, role_id)

        # 校验 agent_id 必须为数字字符串 —— 非数字（如 "abc"）返 400 +
        # AI_AGENT_ID_INVALID（Task 6 review Important #1：原 `int(aid)` 裸调用
        # 抛 ValueError 未被捕获 → HTTP 500 无 errorCode，前端无法 i18n）.
        for aid in req.agent_ids:
            try:
                int(aid)
            except (TypeError, ValueError):
                raise BusinessRuleException(
                    f"agent_id 必须为数字字符串: {aid!r}",
                    error_code="AI_AGENT_ID_INVALID",
                )

        # 去重
        unique_ids = list({int(aid) for aid in req.agent_ids})

        # 校验每个 agent 存在 + 非 shared
        if unique_ids:
            rows = (
                await db.execute(
                    select(AiAgent.agent_id, AiAgent.code).where(
                        AiAgent.agent_id.in_(unique_ids)
                    )
                )
            ).all()
            found_ids = {r[0] for r in rows}
            missing = set(unique_ids) - found_ids
            if missing:
                raise NotFoundException(
                    resource_type="AI Agent",
                    error_code="AI_AGENT_NOT_FOUND",
                )
            # shared 拦截
            shared_hits = [r for r in rows if r[1] == SHARED_AGENT_CODE]
            if shared_hits:
                raise BusinessRuleException(
                    "shared Agent 直通所有用户，无需绑定",
                    error_code="AI_ROLE_AGENT_BIND_SHARED_FORBIDDEN",
                )

        # 全量覆盖：DELETE + INSERT
        await db.execute(delete(RoleAiAgent).where(RoleAiAgent.role_id == role_id))
        for aid in unique_ids:
            db.add(RoleAiAgent(role_id=role_id, agent_id=aid, enabled=True))
        await db.flush()


role_agent_service = RoleAgentService()
