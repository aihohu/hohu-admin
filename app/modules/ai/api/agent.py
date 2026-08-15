"""AI Agent 用户端与管理端接口。

GET /ai/agents 返回当前用户可见的 Agent 列表：
  - 所有身份：enabled Agent + 启用角色的显式 Role-Agent 绑定
  - 并且当前身份对该 Agent 至少有一个可见 Tool

可见性查询统一委托 ``AgentAuthorizationService``，超管和 shared 均无绑定旁路。

管理端使用 ``/ai/admin/agents`` 路径，与用户视角分离，
在 main.py 用 admin_router + 独立 prefix 注册，避免与 /ai/agents 用户视角路径混用）.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_ai_chat_use, require_permissions
from app.core.base_response import ResponseModel
from app.db.session import get_db
from app.modules.ai.schemas.agent_admin import (
    AgentAdminDetailItem,
    AgentAdminListItem,
    AgentAdminUpdateReq,
)
from app.modules.ai.schemas.model import ModelOption
from app.modules.ai.service.agent_admin import agent_admin_service
from app.modules.ai.service.agent_visibility import list_visible_agents
from app.modules.ai.service.model_authorization_service import (
    model_authorization_service,
)
from app.modules.auth.service import get_current_user
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


# ===================== Multi-Agent 管理端接口 =====================
# 决策 #2：admin 视角全量返回（含禁用），与 GET /ai/agents 用户视角分离.
# 决策 #21：admin 端点走 /ai/admin/agents（spec 强约束），与用户视角 /ai/agents 区分.
# Service 不 commit，PUT 端点显式 `await db.commit()`（分层铁律 #9）.
# admin_router 在 main.py 用独立 prefix="/ai/admin/agents" 注册.

admin_router = APIRouter()


@admin_router.get(
    "",
    summary="管理端：列出所有 AI Agent（含禁用）",
    response_model=ResponseModel[list[AgentAdminListItem]],
    dependencies=[Depends(require_permissions("ai:agent:list"))],
)
async def admin_list_agents(
    db: AsyncSession = Depends(get_db),
) -> ResponseModel[list[AgentAdminListItem]]:
    """决策 #23：无 query 参数、无分页，返回全量列表."""
    items = await agent_admin_service.list_agents(db)
    return ResponseModel.success(data=items)


@admin_router.get(
    "/model-options",
    summary="管理端：列出 Agent 可选对话模型",
    response_model=ResponseModel[list[ModelOption]],
    dependencies=[Depends(require_permissions("ai:agent:list"))],
)
async def admin_list_model_options(
    db: AsyncSession = Depends(get_db),
) -> ResponseModel[list[ModelOption]]:
    items = await model_authorization_service.list_model_options(db, tenant_id=0)
    return ResponseModel.success(data=items)


@admin_router.get(
    "/{agent_id}",
    summary="管理端：Agent 详情",
    response_model=ResponseModel[AgentAdminDetailItem],
    dependencies=[Depends(require_permissions("ai:agent:list"))],
)
async def admin_get_agent(
    agent_id: int,
    db: AsyncSession = Depends(get_db),
) -> ResponseModel[AgentAdminDetailItem]:
    """决策 #5：detail 返回含 systemPrompt，list 不返回."""
    item = await agent_admin_service.get_agent(db, agent_id)
    return ResponseModel.success(data=item)


@admin_router.put(
    "/{agent_id}",
    summary="管理端：更新 Agent 配置",
    response_model=ResponseModel[AgentAdminDetailItem],
)
async def admin_update_agent(
    agent_id: int,
    req: AgentAdminUpdateReq,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ResponseModel[AgentAdminDetailItem]:
    """code / is_builtin / agent_id 任一出现即原子拒绝；
    决策 #20：partial update，未传字段保持原值.
    """
    item = await agent_admin_service.update_agent(
        db,
        agent_id,
        req,
        current_user=current_user,
    )
    await db.commit()
    return ResponseModel.success(data=item)
