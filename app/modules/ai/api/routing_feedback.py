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
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.ai.schemas.routing_feedback import (
    FeedbackListItem,
    FeedbackListQuery,
    FeedbackSummary,
    RoutingFeedbackRequest,
)
from app.modules.ai.service.routing_feedback_query import (
    routing_feedback_query_service,
)
from app.modules.ai.service.routing_feedback_service import (
    routing_feedback_service,
)
from app.modules.system.models.user import User

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
    await routing_feedback_service.submit(
        db,
        message_id=message_id,
        request=request,
        user=current_user,
    )
    await db.commit()
    return ResponseModel.success(data=None)


@query_router.get(
    "/summary",
    summary="路由反馈 KPI + Agent 排行",
    response_model=ResponseModel[FeedbackSummary],
    dependencies=[Depends(require_permissions("ai:routing-feedback:list"))],
)
async def get_routing_feedback_summary(
    days: int = Query(7, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> ResponseModel[FeedbackSummary]:
    """返回近 N 天路由反馈 KPI 和错误率最高的 Agent。"""
    summary = await routing_feedback_query_service.summary(db, days=days)
    return ResponseModel.success(data=summary)


@query_router.get(
    "/list",
    summary="路由反馈明细分页",
    response_model=ResponseModel[PageResult[FeedbackListItem]],
    dependencies=[Depends(require_permissions("ai:routing-feedback:list"))],
)
async def get_routing_feedback_list(
    query: FeedbackListQuery = Depends(),
    db: AsyncSession = Depends(get_db),
) -> ResponseModel[PageResult[FeedbackListItem]]:
    """分页查询路由反馈明细，默认只返回 wrong。"""
    items, total = await routing_feedback_query_service.list_items(
        db,
        days=query.days,
        current=query.current,
        size=query.size,
        feedback=query.feedback,
        original_agent=query.original_agent,
        corrected_agent=query.corrected_agent,
    )
    return ResponseModel.success(
        data=PageResult[FeedbackListItem](
            records=items,
            total=total,
            current=query.current,
            size=query.size,
        )
    )
