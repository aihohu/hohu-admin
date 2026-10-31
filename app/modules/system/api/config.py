import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext, TenantLocatorContext
from app.db.authorization_http import (
    control_write,
    get_authorization_runtime,
    release_request_reads,
)
from app.db.session import get_db
from app.modules.auth.service import (
    access_identity,
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
from app.modules.system.service.file_policy_service import file_policy_service

router = APIRouter()
CONFIG_IMPORT_MAX_SIZE_BYTES = 10 * 1024 * 1024


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
    policy = await file_policy_service.resolve(db, "import", tenant=tenant)
    identity = access_identity(_current_user)
    await release_request_reads(db)
    maximum = min(policy.max_bytes, CONFIG_IMPORT_MAX_SIZE_BYTES)
    async with asyncio.timeout(5):
        file_bytes = await file.read(maximum + 1)
    if len(file_bytes) > maximum:
        raise BusinessRuleException(
            "File exceeds the configured import limit",
            error_code="CONFIG_IMPORT_FILE_TOO_LARGE",
        )
    policy.validate_extension(file.filename or "")
    outcome = await get_authorization_runtime().control(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        operation="config.import",
        payload=file_bytes,
    )
    result = outcome["result"] if "cache_refresh" in outcome else outcome
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
    await control_write(
        db, _current_user, operation="config.create", payload=config_in, tenant=tenant
    )
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
    await control_write(
        db,
        _current_user,
        operation="config.update",
        payload=config_in,
        target_id=config_id,
        tenant=tenant,
    )
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
    await control_write(
        db, _current_user, operation="config.delete", target_id=config_id, tenant=tenant
    )
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
    outcome = await control_write(
        db,
        _current_user,
        operation="config.batch_delete",
        payload=int_ids,
        tenant=tenant,
    )
    count = outcome["result"] if isinstance(outcome, dict) else outcome
    return ResponseModel.success(msg=f"成功删除 {count} 个系统配置")
