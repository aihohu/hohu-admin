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
)
from app.core.rbac import is_super_admin
from app.core.tenant import TenantContext
from app.db import authorization_http
from app.db.session import get_db
from app.modules.ai.schemas.operation_log import (
    OperationLogOut,
    OperationLogStatusOut,
    TraceDetailOut,
    TraceListQuery,
    TraceSummaryOut,
)
from app.modules.auth.service import (
    access_identity,
    get_current_tenant_context,
    get_current_user,
)
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
    identity = access_identity(current_user)
    if identity.tenant != tenant:
        raise AuthorizationException(error_code="AI_TRACE_FORBIDDEN")
    await authorization_http.release_request_reads(db)
    page = await authorization_http.get_authorization_runtime().trace.list(
        tenant=identity.tenant, actor_user_version=identity.user_version, query=query
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
    identity = access_identity(current_user)
    if identity.tenant != tenant:
        raise AuthorizationException(error_code="AI_TRACE_FORBIDDEN")
    await authorization_http.release_request_reads(db)
    result = await authorization_http.get_authorization_runtime().trace.detail(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        trace_id=trace_id,
    )
    return ResponseModel.success(data=result)


@router.get("", summary="按 tool_call_id 查 AI 操作日志（SSE 断流兜底轮询用）")
async def get_operation_log(
    tool_call_id: str = Query(..., min_length=3, description="tool_call_id"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
) -> ResponseModel[OperationLogOut | OperationLogStatusOut]:
    """SSE 断流后的兜底查询端点。

    权限：tool_call_id 对应 user_id 本人 / 超管 / ai:trace:view 角色
    字段过滤：只暴露 tool_call_id / tool_name / status / error_code /
             started_at / finished_at / duration_ms
    """
    identity = access_identity(current_user)
    if identity.tenant != tenant:
        raise AuthorizationException(error_code="AI_OPERATION_LOG_FORBIDDEN")
    await authorization_http.release_request_reads(db)
    return await authorization_http.get_authorization_runtime().trace.operation(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        tool_call_id=tool_call_id,
    )
