"""Role-Agent binding service.

职责：
- GET：返回 allAgents + boundAgentIds，不暴露软禁用段（决策 #19）
- PUT：全量覆盖（DELETE + INSERT），normalize 软禁用态为 enabled=True（决策 #15）

跨模块校验 role 存在抛 AI 前缀 errorCode（决策 #18），方便前端 i18n 区分模块归属.
Service 不 commit，由 API 层 `await db.commit()`（CLAUDE.md 硬规则 #9）.

``put_binding`` 与查询使用同一套规范化和校验逻辑，避免 service/API 漂移。
"""

import re

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.tenant import TenantContext
from app.core.tenant_scope import tenant_select
from app.modules.ai.agents.tools.meta import SHARED_AGENT_CODE
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.ai.schemas.role_agent import (
    AgentRow,
    RoleAgentBinding,
    RoleAgentBindReq,
)
from app.modules.system.models.role import Role
from app.modules.system.service.grant_authority import grant_authority_service
from app.modules.system.service.role_delegation_service import (
    role_delegation_service,
)
from app.modules.system.service.role_management_service import role_management_service


class RoleAgentService:
    @staticmethod
    def _normalize_agent_ids(agent_ids: list[str]) -> list[int]:
        """Return one canonical, duplicate-free complete Agent set."""
        if any(
            not isinstance(agent_id, str)
            or re.fullmatch(r"[1-9][0-9]*", agent_id) is None
            for agent_id in agent_ids
        ):
            raise BusinessRuleException(
                "agent_id 必须为正整数规范字符串",
                error_code="AI_AGENT_ID_INVALID",
            )
        normalized = [int(agent_id) for agent_id in agent_ids]
        if len(set(normalized)) != len(normalized):
            raise BusinessRuleException(
                "Agent 集合不能包含重复项",
                error_code="AI_ROLE_AGENT_SET_DUPLICATE",
            )
        return sorted(normalized)

    @staticmethod
    async def _ensure_agents_exist(
        db: AsyncSession,
        agent_ids: list[int],
    ) -> None:
        if not agent_ids:
            return
        found_ids = set(
            (
                await db.execute(
                    select(AiAgent.agent_id).where(AiAgent.agent_id.in_(agent_ids))
                )
            ).scalars()
        )
        if found_ids != set(agent_ids):
            raise NotFoundException(
                resource_type="AI Agent",
                error_code="AI_AGENT_NOT_FOUND",
            )

    async def _get_role_or_404(
        self, db: AsyncSession, role_id: int, *, tenant: TenantContext
    ) -> Role:
        """跨模块校验 role 存在 —— 抛 AI 前缀 errorCode（决策 #18）.

        与 agent_admin._get_agent_or_404 同构（决策 #6 公共 fetch + raise）.
        """
        role = await db.scalar(
            tenant_select(Role, tenant=tenant).where(Role.role_id == role_id)
        )
        if role is None:
            raise NotFoundException(
                resource_type="Role", error_code="AI_ROLE_NOT_FOUND"
            )
        return role

    async def get_binding(
        self,
        db: AsyncSession,
        role_id: int,
        *,
        actor_user_id: int,
        tenant: TenantContext,
    ) -> RoleAgentBinding:
        """GET：返回 allAgents + boundAgentIds.

        - allAgents: 全量 Agent（含禁用、含 shared），按 displayOrder + agentId 排序.
        - boundAgentIds: 该 role 当前 enabled=True 的绑定 agent_id 列表.
        - 不返回 softDisabledAgentIds 段（决策 #19）.
        """
        authority = await grant_authority_service.build(
            db, actor_user_id, tenant=tenant
        )
        if not authority.allows_permission_codes({"system:role:ai-agent-auth"}):
            raise AuthorizationException(
                "权限不足",
                error_code="MISSING_PERMISSION",
            )
        await self._get_role_or_404(db, role_id, tenant=tenant)
        await role_management_service.authorize_role_projection(
            db,
            actor_user_id=actor_user_id,
            role_id=role_id,
            tenant=tenant,
        )

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
                        RoleAiAgent.tenant_id == tenant.tenant_id,
                        RoleAiAgent.role_id == role_id,
                        RoleAiAgent.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )

        visible_agents = [
            agent
            for agent in agents
            if authority.super_admin
            or int(agent.agent_id) in authority.grantable_agent_ids
        ]
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
                for a in visible_agents
            ],
            bound_agent_ids=[str(aid) for aid in bound_rows],
        )

    async def put_binding(
        self,
        db: AsyncSession,
        role_id: int,
        req: RoleAgentBindReq,
        *,
        actor_user_id: int,
        tenant: TenantContext,
        expected_snapshot: dict | None = None,
    ) -> None:
        """Replace the complete binding set through the shared delegation policy.

        Every requested identifier is canonical and unique. The policy validates
        the actor ceiling, affected members, protected identities, and lock-time
        drift before all surviving bindings are normalized to ``enabled=True``.
        """
        unique_ids = self._normalize_agent_ids(req.agent_ids)
        await role_delegation_service.authorize_and_lock_agent_replacement(
            db,
            actor_user_id=actor_user_id,
            role_id=role_id,
            new_agent_ids=unique_ids,
            tenant=tenant,
            expected_snapshot=expected_snapshot,
        )
        await self._ensure_agents_exist(db, unique_ids)

        await db.execute(
            delete(RoleAiAgent).where(
                RoleAiAgent.tenant_id == tenant.tenant_id,
                RoleAiAgent.role_id == role_id,
            )
        )
        for aid in unique_ids:
            db.add(
                RoleAiAgent(
                    tenant_id=tenant.tenant_id,
                    role_id=role_id,
                    agent_id=aid,
                    enabled=True,
                )
            )
        await db.flush()

    async def preview_binding(
        self,
        db: AsyncSession,
        role_id: int,
        agent_ids: list[int],
        *,
        actor_user_id: int,
        tenant: TenantContext,
    ) -> dict:
        """Preview one complete binding replacement without writing."""
        return await role_delegation_service.preview_agent_replacement(
            db,
            actor_user_id=actor_user_id,
            role_id=role_id,
            new_agent_ids=agent_ids,
            tenant=tenant,
        )


role_agent_service = RoleAgentService()
