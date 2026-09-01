from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.cache import cache_delete
from app.core.tenant import TenantContext, TenantLocatorContext
from app.db.session import get_db
from app.modules.auth.service import (
    get_current_tenant_context,
    get_public_tenant_context,
)
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
    tenant: TenantLocatorContext = Depends(get_public_tenant_context),
):
    """获取公开访问的配置项，用于登录页、移动端等场景"""
    data = await config_service.get_public_configs(db, tenant=tenant)
    return ResponseModel.success(data=data)


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[ConfigOut]],
    summary="获取系统配置列表分页",
    dependencies=[Depends(require_permissions("system:config:list"))],
)
async def get_list(
    query: ConfigQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """获取系统配置分页列表"""
    page_data = await config_service.get_list(db, query, tenant=tenant)
    return ResponseModel.success(data=page_data)


@router.get(
    "/export",
    summary="导出系统配置",
    dependencies=[Depends(require_permissions("system:config:export"))],
)
async def export_configs(
    query: ConfigQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """导出系统配置为 Excel 文件"""
    buf = await config_service.export_configs(db, query, tenant=tenant)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=config_export.xlsx"},
    )


@router.post(
    "/import",
    summary="导入系统配置",
    dependencies=[Depends(require_permissions("system:config:import"))],
)
async def import_configs(
    file: Annotated[UploadFile, File(description="Excel 文件")],
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """从 Excel 文件导入系统配置"""
    file_bytes = await file.read()
    result = await config_service.import_configs(db, file_bytes, tenant=tenant)
    await db.commit()
    await cache_delete(pattern=f"tenant:{tenant.tenant_id}:config:*")
    return ResponseModel.success(
        data=result,
        msg=f"导入成功 {result['success']} 条，跳过 {result['skipped']} 条",
    )


@router.post(
    "/add",
    summary="创建系统配置",
    dependencies=[Depends(require_permissions("system:config:add"))],
)
async def add(
    config_in: ConfigCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """创建新的系统配置"""
    await config_service.create(db, config_in, tenant=tenant)
    await db.commit()
    await cache_delete(pattern=f"tenant:{tenant.tenant_id}:config:*")
    return ResponseModel.success(msg="系统配置创建成功")


@router.put(
    "/{config_id}",
    summary="编辑系统配置",
    dependencies=[Depends(require_permissions("system:config:edit"))],
)
async def update(
    config_id: int,
    config_in: ConfigUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """更新系统配置信息"""
    await config_service.update(db, config_id, config_in, tenant=tenant)
    await db.commit()
    await cache_delete(pattern=f"tenant:{tenant.tenant_id}:config:*")
    return ResponseModel.success(msg="系统配置更新成功")


@router.delete(
    "/{config_id}",
    summary="删除系统配置",
    dependencies=[Depends(require_permissions("system:config:delete"))],
)
async def delete(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """删除指定系统配置"""
    await config_service.delete(db, config_id, tenant=tenant)
    await db.commit()
    await cache_delete(pattern=f"tenant:{tenant.tenant_id}:config:*")
    return ResponseModel.success(msg="系统配置删除成功")


@router.post(
    "/batch-delete",
    summary="批量删除系统配置",
    dependencies=[Depends(require_permissions("system:config:batch-delete"))],
)
async def batch_delete(
    ids: list[str],
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """批量删除多个系统配置"""
    int_ids = [int(i) for i in ids]
    count = await config_service.batch_delete(db, int_ids, tenant=tenant)
    await db.commit()
    await cache_delete(pattern=f"tenant:{tenant.tenant_id}:config:*")
    return ResponseModel.success(msg=f"成功删除 {count} 个系统配置")
