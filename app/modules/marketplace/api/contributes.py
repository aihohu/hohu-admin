"""[LOCAL-ONLY] 前端初始化加载 contributes 缓存

部署在本地 HoHu，聚合本机已 enabled 应用的菜单/页面。
如按云端与本地职责拆分，本接口归入 local/contributes.py。
详见 docs/MARKETPLACE-CLOUD-SPLIT.md

原描述：前端初始化加载 contributes 缓存
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import ResponseModel
from app.core.tenant import TenantContext
from app.db.session import get_db
from app.modules.auth.service import get_current_tenant_context, get_current_user
from app.modules.marketplace.capability import require_marketplace_http_capability
from app.modules.marketplace.service.contributes_service import (
    contributes_service,
)
from app.modules.system.models.user import User

router = APIRouter(dependencies=[Depends(require_marketplace_http_capability)])


@router.get(
    "/",
    response_model=ResponseModel[dict],
    summary="获取 contributes（前端初始化）",
)
async def get_contributes(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # noqa: ARG001
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """读 Redis 缓存，miss 时 aggregate 后写缓存"""
    cached = await contributes_service.get_cached(tenant=tenant)
    if cached is None:
        cached = await contributes_service.refresh_cache(db, tenant=tenant)
    return ResponseModel.success(data=cached)
