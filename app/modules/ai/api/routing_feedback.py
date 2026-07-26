"""spec §6.4: POST /ai/messages/{id}/routing-feedback."""

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import ResponseModel
from app.db.session import get_db
from app.modules.ai.schemas.routing_feedback import RoutingFeedbackRequest
from app.modules.ai.service.routing_feedback_service import (
    routing_feedback_service,
)
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

router = APIRouter()  # prefix 由 main.py 的 include_router 提供，避免双重叠加


@router.post(
    "/{message_id}/routing-feedback",
    summary="提交路由反馈",
    response_model=ResponseModel[None],
)
async def submit_routing_feedback(
    request: RoutingFeedbackRequest,
    message_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await routing_feedback_service.submit(
        db,
        message_id=message_id,
        request=request,
        user=current_user,
    )
    await db.commit()
    return ResponseModel.success(data=None)
