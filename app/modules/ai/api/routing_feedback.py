"""Routing feedback API.

两个 router：
- `router` (prefix 由 main.py 挂 `/ai/messages`): POST submit，路径
  `/{message_id}/routing-feedback`.
- `query_router` (prefix 由 main.py 挂 `/ai/routing-feedback`): GET summary + list，
  KPI 汇总和反馈明细查询端点。
"""

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_ai_chat_use, require_permissions
from app.core.base_response import ResponseModel
from app.db import authorization_http
from app.db.session import get_db
from app.modules.ai.schemas.routing_feedback import (
    FeedbackListQuery,
    FeedbackSummary,
    RoutingFeedbackRequest,
)
from app.modules.auth.service import access_identity
from app.modules.system.models.user import User
from app.modules.system.schemas.delivery_projection import (
    DeliveryPage,
    ProjectedFeedback,
)

router = APIRouter()  # prefix 由 main.py 的 include_router 提供，避免双重叠加
query_router = APIRouter()  # /ai/routing-feedback/* 查询端点


@router.post(
    "/{message_id}/routing-feedback",
    summary="提交路由反馈",
    response_model=ResponseModel[None],
)
async def submit_routing_feedback(
    request: RoutingFeedbackRequest,
    message_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_ai_chat_use),
):
    identity = access_identity(current_user)
    await authorization_http.release_request_reads(db)
    await authorization_http.get_authorization_runtime().chat.routing_feedback(
        message_id=message_id,
        feedback=request.feedback,
        corrected_agent_code=request.corrected_agent_code,
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
    )
    return ResponseModel.success(data=None)


@query_router.get(
    "/summary",
    summary="路由反馈 KPI + Agent 排行",
    response_model=ResponseModel[FeedbackSummary],
)
async def get_routing_feedback_summary(
    days: int = Query(7, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permissions("ai:routing-feedback:list")),
) -> ResponseModel[FeedbackSummary]:
    """返回近 N 天路由反馈 KPI 和错误率最高的 Agent。"""
    identity = access_identity(current_user)
    await authorization_http.release_request_reads(db)
    summary = await authorization_http.get_authorization_runtime().delivery.feedback(
        tenant=identity.tenant, actor_user_version=identity.user_version, days=days
    )
    return ResponseModel.success(data=summary)


@query_router.get(
    "/list",
    summary="路由反馈明细分页",
    response_model=ResponseModel[DeliveryPage[ProjectedFeedback]],
    response_model_exclude_unset=True,
)
async def get_routing_feedback_list(
    query: FeedbackListQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permissions("ai:routing-feedback:list")),
) -> ResponseModel[DeliveryPage[ProjectedFeedback]]:
    """分页查询路由反馈明细，默认只返回 wrong。"""
    identity = access_identity(current_user)
    await authorization_http.release_request_reads(db)
    page = await authorization_http.get_authorization_runtime().delivery.feedback(
        tenant=identity.tenant, actor_user_version=identity.user_version, query=query
    )
    return ResponseModel.success(data=page)
