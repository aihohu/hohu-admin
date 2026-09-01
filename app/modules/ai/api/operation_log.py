"""AI 操作日志查询端点。

GET /ai/operation-log?tool_call_id=<tool_call_id>
  用途：前端在 SSE 断流后进行兜底轮询
  权限：本人 / 超管 / 拥有 ai:trace:view 权限码的角色
  字段过滤：只暴露审计元信息（不含 args_summary / result_summary）
"""

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import has_explicit_permission
from app.core.base_response import PageResult, ResponseModel
from app.core.exceptions import (
    AuthorizationException,
    NotFoundException,
)
from app.core.rbac import is_super_admin
from app.core.tenant import TenantContext, resolve_tenant_id
from app.db.session import get_db
from app.modules.ai.constants import AI_CHAT_USE_PERMISSION
from app.modules.ai.schemas.operation_log import (
    OperationLogOut,
    OperationLogStatusOut,
    TraceDetailOut,
    TraceListQuery,
    TraceSummaryOut,
)
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.result_projection_service import (
    result_projection_service,
)
from app.modules.ai.service.trace_service import trace_service
from app.modules.auth.service import get_current_tenant_context, get_current_user
from app.modules.system.models.user import User

AI_TRACE_VIEW_PERM = "ai:trace:view"

logger = logging.getLogger(__name__)

router = APIRouter()


def _ensure_trace_view(user: User) -> None:
    """Enforce the independent Trace audit permission with a stable error code."""
    if is_super_admin(user) or has_explicit_permission(user, AI_TRACE_VIEW_PERM):
        return
    raise AuthorizationException(
        "权限不足",
        error_code="AI_TRACE_FORBIDDEN",
    )


@router.get(
    "/traces",
    response_model=ResponseModel[PageResult[TraceSummaryOut]],
    summary="List tenant-scoped AI Traces",
)
async def list_traces(
    query: TraceListQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
) -> ResponseModel[PageResult[TraceSummaryOut]]:
    _ensure_trace_view(current_user)
    page = await trace_service.list_traces(
        db,
        tenant=tenant,
        query=query,
    )
    return ResponseModel.success(data=page)


@router.get(
    "/traces/{trace_id}",
    response_model=ResponseModel[TraceDetailOut],
    summary="Get one tenant-scoped AI Trace",
)
async def get_trace(
    trace_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
) -> ResponseModel[TraceDetailOut]:
    _ensure_trace_view(current_user)
    detail = await trace_service.get_trace(
        db,
        tenant=tenant,
        trace_id=trace_id,
    )
    return ResponseModel.success(data=detail)


@router.get("", summary="按 tool_call_id 查 AI 操作日志（SSE 断流兜底轮询用）")
async def get_operation_log(
    tool_call_id: str = Query(..., min_length=3, description="tool_call_id"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ResponseModel[OperationLogOut | OperationLogStatusOut]:
    """SSE 断流后的兜底查询端点。

    权限：tool_call_id 对应 user_id 本人 / 超管 / ai:trace:view 角色
    字段过滤：只暴露 tool_call_id / tool_name / status / error_code /
             started_at / finished_at / duration_ms
    """
    # 直接用 service 查（不做 owner 校验，下面统一做）
    tenant_id = resolve_tenant_id(current_user)
    log = await operation_log_service.get_by_tool_call_id(
        db,
        tool_call_id,
        tenant_id=tenant_id,
    )
    if log is None:
        raise NotFoundException("AI 操作日志", error_code="AI_OPERATION_LOG_NOT_FOUND")

    # 权限：本人 / 超管 / ai:trace:view
    is_owner = log.user_id == current_user.user_id
    is_super = is_super_admin(current_user)
    has_perm = has_explicit_permission(current_user, AI_TRACE_VIEW_PERM)
    is_auditor = is_super or has_perm
    if not (is_owner or is_auditor):
        logger.info(
            "AI operation log query denied: user=%s log_user=%s",
            current_user.user_name,
            log.user_id,
        )
        raise AuthorizationException(error_code="AI_OPERATION_LOG_FORBIDDEN")

    if is_auditor:
        return ResponseModel.success(data=OperationLogOut.model_validate(log))

    allowed = False
    if has_explicit_permission(current_user, AI_CHAT_USE_PERMISSION):
        lineage = await result_projection_service.lineage_for_operation_log(db, log)
        allowed = await result_projection_service.authorize_result_projection(
            db,
            current_user,
            owner_user_id=log.user_id,
            lineage=lineage,
        )
    if allowed:
        return ResponseModel.success(data=OperationLogOut.model_validate(log))

    return ResponseModel.success(
        data=OperationLogStatusOut(
            toolCallId=log.tool_call_id,
            status=log.status,
            errorCode="AI_RESULT_PROJECTION_FORBIDDEN",
            finishedAt=log.finished_at,
        )
    )
