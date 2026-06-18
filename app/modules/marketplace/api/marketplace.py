"""公开 API：浏览市场 / 应用详情 / 评分（所有登录用户可见）"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.auth.service import get_current_user
from app.modules.marketplace.schemas.app import AppDetailOut, AppOut, AppQuery
from app.modules.marketplace.schemas.install import InstallCreate, InstallOut
from app.modules.marketplace.schemas.rating import RatingCreate, RatingOut
from app.modules.marketplace.service.app_service import app_service
from app.modules.marketplace.service.install_service import install_service
from app.modules.marketplace.service.rating_service import rating_service
from app.modules.system.models.user import User

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[AppOut]],
    summary="应用列表",
)
async def list_apps(
    query: AppQuery = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """浏览市场：分页 + 分类筛选 + 排序"""
    result = await app_service.list(db, query)
    return ResponseModel.success(
        data=PageResult(
            records=[AppOut.model_validate(r) for r in result.records],
            total=result.total,
            current=result.current,
            size=result.size,
        )
    )


@router.get(
    "/search",
    response_model=ResponseModel[PageResult[AppOut]],
    summary="搜索应用",
)
async def search_apps(
    keyword: str = Query(..., min_length=1),
    current: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """关键词搜索（Phase 1 ILIKE 降级）"""
    result = await app_service.search(db, keyword=keyword, current=current, size=size)
    return ResponseModel.success(
        data=PageResult(
            records=[AppOut.model_validate(r) for r in result.records],
            total=result.total,
            current=result.current,
            size=result.size,
        )
    )


@router.get(
    "/detail/{slug}",
    response_model=ResponseModel[AppDetailOut],
    summary="应用详情",
)
async def get_app_detail(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    app = await app_service.get_by_slug(db, slug=slug)
    data = AppDetailOut.model_validate(app)
    # tags_text 是空格拼接字符串，detail 接口返回数组形式
    if app.tags_text:
        data.tags = app.tags_text.split()
    return ResponseModel.success(data=data)


@router.post(
    "/install",
    response_model=ResponseModel[InstallOut],
    summary="安装应用（仅管理员）",
    dependencies=[Depends(require_permissions("marketplace:install"))],
)
async def install_app(
    req: InstallCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    record = await install_service.install(db, req, user_id=current_user.user_id)
    await db.commit()
    return ResponseModel.success(data=InstallOut.model_validate(record))


@router.post(
    "/uninstall/{app_id}",
    response_model=ResponseModel[None],
    summary="卸载应用（仅管理员）",
    dependencies=[Depends(require_permissions("marketplace:install"))],
)
async def uninstall_app(
    app_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await install_service.uninstall(db, app_id=app_id, user_id=current_user.user_id)
    await db.commit()
    return ResponseModel.success()


@router.post(
    "/rating",
    response_model=ResponseModel[RatingOut],
    summary="评分（需先安装，Phase 1 简化为所有登录用户可评）",
)
async def create_rating(
    req: RatingCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rating = await rating_service.create(db, req, user_id=current_user.user_id)
    await db.commit()
    return ResponseModel.success(data=RatingOut.model_validate(rating))
