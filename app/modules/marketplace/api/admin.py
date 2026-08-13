"""[CLOUD-ONLY] 审核 API（approve / reject）

部署在云市场。本地 HoHu 不挂此 router——本地直接信任 published 应用。
如按云端与本地职责拆分，本接口归入 cloud/admin.py。
详见 docs/MARKETPLACE-CLOUD-SPLIT.md

原描述：管理员 API：审核 / 启用禁用应用
"""

from fastapi import APIRouter, Depends, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.auth.service import get_current_user
from app.modules.marketplace.models import App, AppVersion
from app.modules.marketplace.schemas.review import ReviewDetail, ReviewListItem
from app.modules.marketplace.service.review_service import review_service
from app.modules.system.models.user import User

router = APIRouter()


@router.get(
    "/reviews",
    response_model=ResponseModel[PageResult[ReviewListItem]],
    summary="审核列表（pending/approved/rejected）",
    dependencies=[Depends(require_permissions("marketplace:review"))],
)
async def list_reviews(
    current: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    status: str = Query("pending", pattern="^(pending|approved|rejected|all)$"),
    app_slug: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    data = await review_service.list_reviews(
        db, current=current, size=size, status=status, app_slug=app_slug
    )
    return ResponseModel.success(
        data=PageResult(
            records=[ReviewListItem.model_validate(r) for r in data["records"]],
            total=data["total"],
            current=data["current"],
            size=data["size"],
        )
    )


@router.get(
    "/review/{review_id}",
    response_model=ResponseModel[ReviewDetail],
    summary="审核详情（含 manifest）",
    dependencies=[Depends(require_permissions("marketplace:review"))],
)
async def get_review(
    review_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    data = await review_service.get_detail(db, review_id=review_id)
    return ResponseModel.success(data=ReviewDetail.model_validate(data))


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
        # refresh 避免 onupdate=func.now() 触发 lazy-load（同 enable bug）
        await db.refresh(version)
    # 把 app.status 改为 published + 更新 current_version_id
    app = await db.get(App, review.app_id)
    if app is not None:
        app.status = "published"
        app.current_version_id = review.version_id
        await db.refresh(app)
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
        await db.refresh(version)
    app = await db.get(App, review.app_id)
    if app is not None and app.status == "reviewing":
        app.status = "rejected"
        await db.refresh(app)
    await db.commit()
    return ResponseModel.success()
