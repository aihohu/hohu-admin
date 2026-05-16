from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.base_response import PageResult, ResponseModel
from app.core.cache import cache_delete
from app.db.session import get_db
from app.modules.system.models.user import User
from app.modules.system.schemas.config import (
    ConfigCreate,
    ConfigOut,
    ConfigQuery,
    ConfigUpdate,
)
from app.modules.system.service.config_service import config_service

router = APIRouter()


@router.get("/public", summary="获取公开配置（无需鉴权）")
async def get_public_configs(
    db: AsyncSession = Depends(get_db),
):
    """获取公开访问的配置项，用于登录页、移动端等场景"""
    data = await config_service.get_public_configs(db)
    return ResponseModel.success(data=data)


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[ConfigOut]],
    summary="获取系统配置列表分页",
)
async def get_list(
    query: ConfigQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """获取系统配置分页列表"""
    page_data = await config_service.get_list(db, query)
    return ResponseModel.success(data=page_data)


@router.post("/add", summary="创建系统配置")
async def add(
    config_in: ConfigCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """创建新的系统配置"""
    await config_service.create(db, config_in)
    await db.commit()
    await cache_delete(pattern="config:*")
    return ResponseModel.success(msg="系统配置创建成功")


@router.put("/{config_id}", summary="编辑系统配置")
async def update(
    config_id: int,
    config_in: ConfigUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """更新系统配置信息"""
    await config_service.update(db, config_id, config_in)
    await db.commit()
    await cache_delete(pattern="config:*")
    return ResponseModel.success(msg="系统配置更新成功")


@router.delete("/{config_id}", summary="删除系统配置")
async def delete(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """删除指定系统配置"""
    await config_service.delete(db, config_id)
    await db.commit()
    await cache_delete(pattern="config:*")
    return ResponseModel.success(msg="系统配置删除成功")


@router.post("/batch-delete", summary="批量删除系统配置")
async def batch_delete(
    ids: list[str],
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """批量删除多个系统配置"""
    int_ids = [int(i) for i in ids]
    count = await config_service.batch_delete(db, int_ids)
    await db.commit()
    await cache_delete(pattern="config:*")
    return ResponseModel.success(msg=f"成功删除 {count} 个系统配置")
