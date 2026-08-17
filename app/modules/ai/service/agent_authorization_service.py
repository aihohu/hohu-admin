"""AI Agent 授权策略的单一入口。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED
from app.core.exceptions import AuthorizationException
from app.modules.ai.agents.safety.ai_config import get_ai_config_str_list
from app.modules.ai.agents.tools.registry import compute_available_tools
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.auth.permission_collect import collect_user_permission_codes
from app.modules.system.models.user import User


class AgentAuthorizationService:
    """统一执行 Agent 全局开关、角色绑定与 Tool 可见性校验。"""

    def tool_permissions(self, user: User) -> set[str]:
        """返回 Gateway 与 Agent 可见性共用的 Tool 权限集合。"""
        return collect_user_permission_codes(user)

    async def grantable_agent_ids(
        self,
        db: AsyncSession,
        user: User,
    ) -> set[int]:
        """Return every explicit potential Agent grant from enabled roles.

        Only active Role-Agent bindings contribute delegation authority. The
        Agent's global enabled flag intentionally does not filter this set, so
        an explicitly bound disabled Agent can still be removed safely.
        """
        role_ids = self._enabled_role_ids(user)
        by_role = await self.grantable_agent_ids_by_role_ids(db, role_ids)
        return {agent_id for agent_ids in by_role.values() for agent_id in agent_ids}

    async def grantable_agent_ids_by_role_ids(
        self,
        db: AsyncSession,
        role_ids: list[int] | tuple[int, ...] | set[int],
    ) -> dict[int, set[int]]:
        """Return active Role-Agent grants grouped by explicit role identifier."""
        normalized_role_ids = {int(role_id) for role_id in role_ids}
        result: dict[int, set[int]] = {
            role_id: set() for role_id in normalized_role_ids
        }
        if not normalized_role_ids:
            return result
        rows = (
            await db.execute(
                select(RoleAiAgent.role_id, RoleAiAgent.agent_id).where(
                    RoleAiAgent.role_id.in_(normalized_role_ids),
                    RoleAiAgent.enabled.is_(True),
                )
            )
        ).all()
        for role_id, agent_id in rows:
            result[int(role_id)].add(int(agent_id))
        return result

    async def grantable_agent_ids_for_role_ids(
        self,
        db: AsyncSession,
        role_ids: list[int] | tuple[int, ...] | set[int],
    ) -> set[int]:
        """Return the active Role-Agent union for an explicit role set."""
        by_role = await self.grantable_agent_ids_by_role_ids(db, role_ids)
        return {agent_id for agent_ids in by_role.values() for agent_id in agent_ids}

    @staticmethod
    def _enabled_role_ids(user: User) -> list[int]:
        return [
            role.role_id for role in (user.roles or []) if role.status == STATUS_ENABLED
        ]

    async def _bound_enabled_agents(
        self,
        db: AsyncSession,
        user: User,
    ) -> list[AiAgent]:
        role_ids = self._enabled_role_ids(user)
        if not role_ids:
            return []
        result = await db.execute(
            select(AiAgent)
            .join(RoleAiAgent, RoleAiAgent.agent_id == AiAgent.agent_id)
            .where(
                AiAgent.enabled.is_(True),
                RoleAiAgent.enabled.is_(True),
                RoleAiAgent.role_id.in_(role_ids),
            )
            .order_by(AiAgent.display_order, AiAgent.agent_id)
        )
        seen: set[int] = set()
        agents: list[AiAgent] = []
        for agent in result.scalars().all():
            if agent.agent_id not in seen:
                seen.add(agent.agent_id)
                agents.append(agent)
        return agents

    async def list_agents(
        self,
        db: AsyncSession,
        user: User,
    ) -> list[AiAgent]:
        """列出通过完整 Agent Policy 的候选 Agent。"""
        perms = self.tool_permissions(user)
        enabled_extra = await get_ai_config_str_list(
            db,
            "ai:enabled_tools",
            default=[],
        )
        return [
            agent
            for agent in await self._bound_enabled_agents(db, user)
            if compute_available_tools(
                perms,
                agent.code,
                enabled_extra=enabled_extra,
            )
        ]

    async def authorize_agent_access(
        self,
        db: AsyncSession,
        user: User,
        agent_code: str,
        *,
        error_code: str = "AI_AGENT_FORBIDDEN",
    ) -> AiAgent:
        """校验并返回 Agent；失败时不泄露具体缺失的授权层。"""
        agent = next(
            (
                item
                for item in await self.list_agents(db, user)
                if item.code == agent_code
            ),
            None,
        )
        if agent is None:
            raise AuthorizationException(
                "当前用户不可使用该 AI Agent",
                error_code=error_code,
            )
        return agent


agent_authorization_service = AgentAuthorizationService()
