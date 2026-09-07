"""AI Agent 租户用户接口。

GET /ai/agents 返回当前用户可见的 Agent 列表：
  - 所有身份：enabled Agent + 启用角色的显式 Role-Agent 绑定
  - 并且当前身份对该 Agent 至少有一个可见 Tool

可见性查询统一委托 ``AgentAuthorizationService``，超管和 shared 均无绑定旁路。

全局管理入口只存在于 ``/platform/ai/agents`` 控制面。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_ai_chat_use
from app.core.base_response import ResponseModel
from app.db.session import get_db
from app.modules.ai.service.agent_visibility import list_visible_agents
from app.modules.system.models.user import User

router = APIRouter()


@router.get("", summary="列出当前用户可用的 AI Agent")
async def list_agents(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_ai_chat_use),
) -> ResponseModel[list[dict]]:
    """列出当前用户可见的 Agent，查询逻辑由 service 统一维护。

    Returns:
        agents: [{code, name, description, modelPreference, displayOrder}, ...]
        按 displayOrder 升序
    """
    agents = await list_visible_agents(db, current_user)
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
