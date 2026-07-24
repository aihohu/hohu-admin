"""AI Agent 列表端点（spec §4.3 / §10.3）

GET /ai/agents 返回当前用户可见的 Agent 列表：
  - 超管：所有 enabled=True 的 Agent
  - 普通用户：role_ai_agent 关联 + shared Agent（直通）
"""

# ruff: noqa: PLC0415  inline import 避免循环（meta 模块间循环依赖）

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import ResponseModel
from app.core.rbac import is_super_admin
from app.db.session import get_db
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

router = APIRouter()


@router.get("", summary="列出当前用户可用的 AI Agent")
async def list_agents(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ResponseModel[list[dict]]:
    """spec §4.3 / §10.3: 列出当前用户可见的 Agent

    可见规则：
      - 超管：所有 enabled=True 的 Agent
      - 普通用户：role_ai_agent 关联（enabled=True 且 role 启用）+ shared Agent（直通）

    Returns:
        agents: [{code, name, description, modelPreference, displayOrder}, ...]
        按 displayOrder 升序
    """
    if is_super_admin(current_user):
        stmt = (
            select(AiAgent)
            .where(AiAgent.enabled.is_(True))
            .order_by(AiAgent.display_order.asc())
        )
    else:
        # 普通用户：role_ai_agent JOIN ai_agent，且 agent.enabled=True
        # shared Agent 永远直通（SHARED_AGENT_CODE）
        from app.modules.ai.agents.tools.meta import SHARED_AGENT_CODE

        user_role_ids = [r.role_id for r in current_user.roles if r.status == "1"]
        stmt = (
            select(AiAgent)
            .outerjoin(RoleAiAgent, RoleAiAgent.agent_id == AiAgent.agent_id)
            .where(
                AiAgent.enabled.is_(True),
                (
                    RoleAiAgent.role_id.in_(user_role_ids)
                    if user_role_ids
                    else AiAgent.code == SHARED_AGENT_CODE
                )
                | (AiAgent.code == SHARED_AGENT_CODE),
            )
            .order_by(AiAgent.display_order.asc())
            .distinct()
        )

    result = await db.execute(stmt)
    agents = result.scalars().all()
    return ResponseModel.success(
        data=[
            {
                "code": a.code,
                "name": a.name,
                "description": a.description,
                "modelPreference": a.model_preference,
                "displayOrder": a.display_order,
            }
            for a in agents
        ]
    )
