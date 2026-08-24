"""Role-Agent binding endpoints.

URL 走 /ai/role-agent 而非 /system/role（决策 #17）：
表归 ai 模块，service 也归 ai 模块，避免 system → ai 跨模块依赖.

Service 不 commit，PUT 端点显式 `await db.commit()`（分层铁律 #9）.
GET 端点返回 typed ``ResponseModel[RoleAgentBinding]``，
（response_model=ResponseModel[dict] 是反例，会让 FastAPI schema 不精确）.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_permissions
from app.core.base_response import ResponseModel
from app.db.session import get_db
from app.modules.ai.schemas.role_agent import RoleAgentBinding, RoleAgentBindReq
from app.modules.ai.service.role_agent import role_agent_service
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

router = APIRouter()


@router.get(
    "/{role_id}",
    summary="获取 Role 已绑 Agent 列表 + 全量 Agent 树",
    response_model=ResponseModel[RoleAgentBinding],
    dependencies=[Depends(require_permissions("system:role:ai-agent-auth"))],
)
async def get_role_agent_binding(
    role_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ResponseModel[RoleAgentBinding]:
    """返回 allAgents（全量）和 boundAgentIds（绑定列表）。

    决策 #19: 不返回 softDisabledAgentIds 段；决策 #18: role 不存在返
    AI_ROLE_NOT_FOUND（跨模块 AI 前缀）.
    """
    binding = await role_agent_service.get_binding(
        db,
        role_id,
        actor_user_id=current_user.user_id,
    )
    return ResponseModel.success(data=binding)


@router.put(
    "/{role_id}",
    summary="全量覆盖 Role 的 Agent 绑定",
    response_model=ResponseModel[None],
    dependencies=[Depends(require_permissions("system:role:ai-agent-auth"))],
)
async def put_role_agent_binding(
    role_id: int,
    req: RoleAgentBindReq,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ResponseModel[None]:
    """决策 #15：全量覆盖（DELETE + INSERT），normalize 软禁用态为 enabled=True.

    PUT 端点复用 service 层规范化和边界校验，避免 API 与 service 漂移。
    """
    await role_agent_service.put_binding(
        db,
        role_id,
        req,
        actor_user_id=current_user.user_id,
    )
    await db.commit()
    return ResponseModel.success(data=None)
