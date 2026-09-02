"""[SPLIT-ME] 混合 router：browse 部分 cloud-only，install/enable 部分 local-only

如按云端与本地职责物理拆分，可按 endpoint 切到两个文件：
- cloud/browse.py: GET /list, /search, /detail/{slug}, /{slug}/manifest
- local/install.py: POST /install, /uninstall/{slug}, /enable/{slug}, /disable/{slug}, GET /installed
- local/rating.py: POST /rating, PUT /rating/{app_id}, DELETE /rating/{app_id}

详见 docs/MARKETPLACE-CLOUD-SPLIT.md

原描述：公开 API：浏览市场 / 应用详情 / 评分（所有登录用户可见）
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.tenant import TenantContext
from app.db.session import get_db
from app.modules.auth.service import get_current_tenant_context, get_current_user
from app.modules.marketplace.capability import require_marketplace_http_capability
from app.modules.marketplace.exceptions import AppNotFoundException
from app.modules.marketplace.models import AppVersion
from app.modules.marketplace.schemas.app import AppDetailOut, AppOut, AppQuery
from app.modules.marketplace.schemas.install import (
    InstallCreate,
    InstallOut,
    InstallQuery,
)
from app.modules.marketplace.schemas.rating import (
    RatingCreate,
    RatingOut,
    RatingUpdate,
)
from app.modules.marketplace.service.app_service import app_service
from app.modules.marketplace.service.install_service import install_service
from app.modules.marketplace.service.rating_service import rating_service
from app.modules.system.models.user import User

router = APIRouter(dependencies=[Depends(require_marketplace_http_capability)])


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[AppOut]],
    summary="应用列表",
)
async def list_apps(
    query: AppQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # noqa: ARG001
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """浏览市场：分页 + 分类筛选 + 排序（需登录）"""
    result = await app_service.list(db, query, tenant=tenant)
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
    current_user: User = Depends(get_current_user),  # noqa: ARG001
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """关键词搜索；全文索引不可用时使用 ILIKE。"""
    result = await app_service.search(
        db, keyword=keyword, current=current, size=size, tenant=tenant
    )
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
    current_user: User = Depends(get_current_user),  # noqa: ARG001
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    app = await app_service.get_by_slug(db, slug=slug, tenant=tenant)
    data = AppDetailOut.model_validate(app)
    # tags_text 是空格拼接字符串，detail 接口返回数组形式
    if app.tags_text:
        data.tags = app.tags_text.split()
    return ResponseModel.success(data=data)


@router.get(
    "/{slug}/manifest",
    response_model=ResponseModel[dict],
    summary="获取应用完整 manifest",
)
async def get_app_manifest(
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # noqa: ARG001
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """前端 LowcodeRenderer 用：返回 current_version 的完整 manifest"""
    app = await app_service.get_by_slug(db, slug=slug, tenant=tenant)
    if app.current_version_id is None:
        raise AppNotFoundException(slug=f"{slug} (no published version)")
    version = await db.scalar(
        select(AppVersion).where(
            AppVersion.id == app.current_version_id,
            AppVersion.app_id == app.id,
        )
    )
    if version is None:
        raise AppNotFoundException(slug=slug)
    return ResponseModel.success(data=version.manifest)


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
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    record = await install_service.install(
        db, req, user_id=current_user.user_id, tenant=tenant
    )
    await db.commit()
    return ResponseModel.success(data=InstallOut.model_validate(record))


@router.post(
    "/uninstall/{slug}",
    response_model=ResponseModel[None],
    summary="卸载应用（仅管理员）",
    dependencies=[Depends(require_permissions("marketplace:install"))],
)
async def uninstall_app(
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    # 统一用 slug 路径，保持与 install 一致；先查 app 拿 id
    app = await app_service.get_by_slug(db, slug=slug, tenant=tenant)
    await install_service.uninstall(
        db, app_id=app.id, user_id=current_user.user_id, tenant=tenant
    )
    await db.commit()
    return ResponseModel.success()


@router.post(
    "/enable/{slug}",
    response_model=ResponseModel[InstallOut],
    summary="启用应用（仅管理员）",
    dependencies=[Depends(require_permissions("marketplace:install"))],
)
async def enable_app(
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # noqa: ARG001
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    app = await app_service.get_by_slug(db, slug=slug, tenant=tenant)
    record = await install_service.enable(db, app_id=app.id, tenant=tenant)
    await db.commit()
    return ResponseModel.success(data=InstallOut.model_validate(record))


@router.post(
    "/disable/{slug}",
    response_model=ResponseModel[InstallOut],
    summary="禁用应用（仅管理员）",
    dependencies=[Depends(require_permissions("marketplace:install"))],
)
async def disable_app(
    slug: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # noqa: ARG001
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    app = await app_service.get_by_slug(db, slug=slug, tenant=tenant)
    record = await install_service.disable(db, app_id=app.id, tenant=tenant)
    await db.commit()
    return ResponseModel.success(data=InstallOut.model_validate(record))


@router.get(
    "/installed",
    response_model=ResponseModel[PageResult[InstallOut]],
    summary="已安装应用列表（当前用户/管理员可见）",
)
async def list_installed(
    query: InstallQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # noqa: ARG001
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    result = await install_service.list_installed(db, query, tenant=tenant)
    return ResponseModel.success(
        data=PageResult(
            records=[InstallOut.model_validate(r) for r in result.records],
            total=result.total,
            current=result.current,
            size=result.size,
        )
    )


@router.post(
    "/rating",
    response_model=ResponseModel[RatingOut],
    summary="评分（需先安装，当前所有登录用户可评）",
)
async def create_rating(
    req: RatingCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    rating = await rating_service.create(
        db, req, user_id=current_user.user_id, tenant=tenant
    )
    await db.commit()
    return ResponseModel.success(data=RatingOut.model_validate(rating))


@router.put(
    "/rating/{app_id}",
    response_model=ResponseModel[RatingOut],
    summary="修改评分",
)
async def update_rating(
    app_id: int,
    req: RatingUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    rating = await rating_service.update(
        db,
        app_id=app_id,
        user_id=current_user.user_id,
        rating=req.rating,
        comment=req.comment,
        tenant=tenant,
    )
    await db.commit()
    return ResponseModel.success(data=RatingOut.model_validate(rating))


@router.delete(
    "/rating/{app_id}",
    response_model=ResponseModel[None],
    summary="删除评分",
)
async def delete_rating(
    app_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    await rating_service.delete(
        db, app_id=app_id, user_id=current_user.user_id, tenant=tenant
    )
    await db.commit()
    return ResponseModel.success()
