"""前端初始化加载 contributes 缓存"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import ResponseModel
from app.db.session import get_db
from app.modules.auth.service import get_current_user
from app.modules.marketplace.service.contributes_service import (
    contributes_service,
)
from app.modules.system.models.user import User

router = APIRouter()


@router.get(
    "/",
    response_model=ResponseModel[dict],
    summary="获取 contributes（前端初始化）",
)
async def get_contributes(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),  # noqa: ARG001
):
    """读 Redis 缓存，miss 时 aggregate 后写缓存"""
    cached = await contributes_service.get_cached(tenant_id=0)
    if cached is None:
        cached = await contributes_service.refresh_cache(db, tenant_id=0)
    return ResponseModel.success(data=cached)
