"""AI 操作日志查询端点。

GET /ai/operation-log?tool_call_id=<tool_call_id>
  用途：前端在 SSE 断流后进行兜底轮询
  权限：本人 / 超管 / 拥有 ai:trace:view 权限码的角色
  字段过滤：只暴露审计元信息（不含 args_summary / result_summary）
"""

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED
from app.core.base_response import ResponseModel
from app.core.exceptions import (
    AuthorizationException,
    NotFoundException,
)
from app.core.rbac import is_super_admin
from app.db.session import get_db
from app.modules.ai.schemas.operation_log import OperationLogOut
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

AI_TRACE_VIEW_PERM = "ai:trace:view"

logger = logging.getLogger(__name__)

router = APIRouter()


def _has_trace_view_perm(user: User) -> bool:
    """检查 user 是否拥有 ai:trace:view 权限码（启用角色）"""
    for role in user.roles:
        if role.status != STATUS_ENABLED:
            continue
        for menu in role.menus:
            if menu.permission == AI_TRACE_VIEW_PERM:
                return True
    return False


@router.get("", summary="按 tool_call_id 查 AI 操作日志（SSE 断流兜底轮询用）")
async def get_operation_log(
    tool_call_id: str = Query(..., min_length=3, description="tool_call_id"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ResponseModel[OperationLogOut]:
    """SSE 断流后的兜底查询端点。

    权限：tool_call_id 对应 user_id 本人 / 超管 / ai:trace:view 角色
    字段过滤：只暴露 tool_call_id / tool_name / status / error_code /
             started_at / finished_at / duration_ms
    """
    # 直接用 service 查（不做 owner 校验，下面统一做）
    log = await operation_log_service.get_by_tool_call_id(db, tool_call_id)
    if log is None:
        raise NotFoundException("AI 操作日志", error_code="AI_OPERATION_LOG_NOT_FOUND")

    # 权限：本人 / 超管 / ai:trace:view
    is_owner = log.user_id == current_user.user_id
    is_super = is_super_admin(current_user)
    has_perm = _has_trace_view_perm(current_user)
    if not (is_owner or is_super or has_perm):
        logger.info(
            "AI operation log query denied: user=%s log_user=%s",
            current_user.user_name,
            log.user_id,
        )
        raise AuthorizationException(error_code="AI_OPERATION_LOG_FORBIDDEN")

    return ResponseModel.success(data=OperationLogOut.model_validate(log))
