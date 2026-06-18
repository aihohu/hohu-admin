"""管理员 API：审核 / 启用禁用应用"""

from fastapi import APIRouter, Depends, Form
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_permissions
from app.core.base_response import ResponseModel
from app.db.session import get_db
from app.modules.auth.service import get_current_user
from app.modules.marketplace.models import App, AppVersion
from app.modules.marketplace.service.review_service import review_service
from app.modules.system.models.user import User

router = APIRouter()


@router.post(
    "/review/{review_id}/approve",
    response_model=ResponseModel[None],
    summary="审核通过",
    dependencies=[Depends(require_permissions("marketplace:review"))],
)
async def approve_review(
    review_id: int,
    comment: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    review = await review_service.human_review(
        db,
        review_id=review_id,
        reviewer_id=current_user.user_id,
        approved=True,
        comment=comment,
    )
    # 把对应 app_version.review_status 改为 approved
    version = await db.get(AppVersion, review.version_id)
    if version is not None:
        version.review_status = "approved"
    # 把 app.status 改为 published + 更新 current_version_id
    app = await db.get(App, review.app_id)
    if app is not None:
        app.status = "published"
        app.current_version_id = review.version_id
    await db.commit()
    return ResponseModel.success()


@router.post(
    "/review/{review_id}/reject",
    response_model=ResponseModel[None],
    summary="审核拒绝",
    dependencies=[Depends(require_permissions("marketplace:review"))],
)
async def reject_review(
    review_id: int,
    comment: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    review = await review_service.human_review(
        db,
        review_id=review_id,
        reviewer_id=current_user.user_id,
        approved=False,
        comment=comment,
    )
    version = await db.get(AppVersion, review.version_id)
    if version is not None:
        version.review_status = "rejected"
    app = await db.get(App, review.app_id)
    if app is not None and app.status == "reviewing":
        app.status = "rejected"
    await db.commit()
    return ResponseModel.success()
