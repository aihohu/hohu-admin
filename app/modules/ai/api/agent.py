"""AI Agent 列表端点（spec §4.3 / §10.3）

GET /ai/agents 返回当前用户可见的 Agent 列表：
  - 超管：所有 enabled=True 的 Agent
  - 普通用户：role_ai_agent 关联 + shared Agent（直通）

可见性查询逻辑下沉到 service/agent_visibility.py（spec §6.3 单一真相源）.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import ResponseModel
from app.db.session import get_db
from app.modules.ai.service.agent_visibility import list_visible_agents
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

router = APIRouter()


@router.get("", summary="列出当前用户可用的 AI Agent")
async def list_agents(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ResponseModel[list[dict]]:
    """spec §4.3 / §10.3: 列出当前用户可见的 Agent（query 逻辑下沉到 service）.

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
