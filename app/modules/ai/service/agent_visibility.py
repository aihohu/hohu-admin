"""Agent 可见性逻辑的单一真相源。

API 层（GET /ai/agents）+ Service 层（chat.py supervisor 候选集 / routing feedback 校验）共用.
抽出独立 service 模块避免 service → api 反向依赖（CLAUDE.md 硬规则 #9 分层铁律）.
"""

# ruff: noqa: PLC0415  inline import 避免循环（meta 模块间循环依赖）

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import is_super_admin
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.system.models.user import User


async def list_visible_agents(db: AsyncSession, current_user: User) -> list[AiAgent]:
    """返回当前用户可见的 enabled=True Agent（与 GET /ai/agents 一致）.

    可见性规则：
    - 超管：所有 enabled=True Agent
    - 普通用户：role_ai_agent 关联（role.status='1'）+ shared 直通

    返回 ORM 对象列表（不是 dict），调用方按需序列化.
    """
    from app.modules.ai.agents.tools.meta import SHARED_AGENT_CODE

    if is_super_admin(current_user):
        stmt = (
            select(AiAgent)
            .where(AiAgent.enabled.is_(True))
            .order_by(AiAgent.display_order, AiAgent.agent_id)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    # 普通用户：role_ai_agent 关联 OR shared 直通
    role_ids = [r.role_id for r in (current_user.roles or []) if r.status == "1"]
    if not role_ids:
        stmt = (
            select(AiAgent)
            .where(AiAgent.enabled.is_(True), AiAgent.code == SHARED_AGENT_CODE)
            .order_by(AiAgent.display_order, AiAgent.agent_id)
        )
    else:
        stmt = (
            select(AiAgent)
            .outerjoin(RoleAiAgent, RoleAiAgent.agent_id == AiAgent.agent_id)
            .where(
                AiAgent.enabled.is_(True),
                or_(
                    AiAgent.code == SHARED_AGENT_CODE,
                    RoleAiAgent.role_id.in_(role_ids),
                ),
            )
            .order_by(AiAgent.display_order, AiAgent.agent_id)
        )
    result = await db.execute(stmt)
    # DISTINCT 防止 shared 重复（既满足 code=SHARED 又被 role 关联的边界）
    seen: set[int] = set()
    out: list[AiAgent] = []
    for a in result.scalars().all():
        if a.agent_id not in seen:
            seen.add(a.agent_id)
            out.append(a)
    return out
