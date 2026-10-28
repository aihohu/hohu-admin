from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import is_super_admin, require_ai_chat_use, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext
from app.db import authorization_http
from app.db.session import get_db
from app.modules.auth.service import (
    access_identity,
    get_current_tenant_context,
    get_current_user,
)
from app.modules.system.models.user import User
from app.modules.system.schemas.file import FileOut, FileQuery
from app.modules.system.service.file_service import file_service

router = APIRouter()


@router.get("/chat-image", summary="读取当前上传者的 AI 聊天图片")
async def read_chat_image(
    file_url: str = Query(alias="fileUrl", max_length=2048),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_ai_chat_use),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    if not settings.AI_MODULE_ENABLED:
        raise BusinessRuleException("AI 助手已关闭", error_code="AI_MODULE_DISABLED")
    content, mime = await file_service.read_chat_image(db, file_url, tenant=tenant)
    return Response(
        content=content,
        media_type=mime,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post(
    "/upload",
    response_model=ResponseModel[FileOut],
    summary="单文件上传",
    description="上传单个文件，可选关联业务类型和业务ID",
    dependencies=[Depends(require_permissions("system:file:upload"))],
)
async def upload(
    file: Annotated[UploadFile, File(description="上传的文件")],
    business_type: str | None = Form(None, description="业务类型(如product、avatar)"),
    business_id: int | None = Form(None, description="业务记录ID"),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """上传单个文件"""
    identity = access_identity(_current_user)
    if identity.tenant != tenant:
        raise authorization_http._order_error()
    await authorization_http.release_request_reads(db)
    records = await authorization_http.get_authorization_runtime().upload.upload(
        files=(file,),
        business_type=business_type,
        business_id=business_id,
        tenant=tenant,
        actor_user_version=identity.user_version,
    )
    return ResponseModel.success(data=records[0], msg="文件上传成功")


@router.post(
    "/batch-upload",
    response_model=ResponseModel[list[FileOut]],
    summary="多文件上传",
    description="批量上传多个文件, 可选关联业务类型和业务ID",
    dependencies=[Depends(require_permissions("system:file:upload"))],
)
async def batch_upload(
    files: Annotated[list[UploadFile], File(description="上传的文件列表")],
    business_type: str | None = Form(None, description="业务类型"),
    business_id: int | None = Form(None, description="业务记录ID"),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """批量上传文件"""
    identity = access_identity(_current_user)
    if identity.tenant != tenant:
        raise authorization_http._order_error()
    await authorization_http.release_request_reads(db)
    file_records = await authorization_http.get_authorization_runtime().upload.upload(
        files=files,
        business_type=business_type,
        business_id=business_id,
        tenant=tenant,
        actor_user_version=identity.user_version,
    )
    return ResponseModel.success(
        data=file_records, msg=f"成功上传 {len(file_records)} 个文件"
    )


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[FileOut]],
    summary="获取文件列表",
    description="分页查询文件列表，支持按文件名、业务类型、扩展名筛选",
    dependencies=[Depends(require_permissions("system:file:list"))],
)
async def get_list(
    query: FileQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """获取文件分页列表"""
    page_data = await file_service.get_list(
        db,
        query,
        tenant=tenant,
    )
    return ResponseModel.success(data=page_data)


@router.get(
    "/{file_id}",
    response_model=ResponseModel[FileOut],
    summary="获取文件详情",
    description="根据文件ID获取文件详细信息",
)
async def get_by_id(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """获取文件详情"""
    admin = is_super_admin(_current_user)
    file_record = await file_service.get_by_id(
        db,
        file_id,
        tenant=tenant,
        owner_user_id=_current_user.user_id,
        is_admin=admin,
    )
    return ResponseModel.success(data=file_record)


@router.delete(
    "/{file_id}",
    summary="删除文件",
    description="删除指定文件及磁盘文件。普通用户仅能删除自己上传的，超管可删任何文件。",
)
async def delete(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """删除文件（ownership 检查在 service）"""
    identity = access_identity(current_user)
    if identity.tenant != tenant:
        raise authorization_http._order_error()
    await authorization_http.release_request_reads(db)
    await authorization_http.get_authorization_runtime().upload.delete(
        identifiers=(file_id,),
        tenant=tenant,
        actor_user_version=identity.user_version,
    )
    return ResponseModel.success(msg="文件删除成功")


@router.post(
    "/batch-delete",
    summary="批量删除文件",
    description="批量删除多个文件（管理员视角，需要 system:file:delete 权限）",
    dependencies=[Depends(require_permissions("system:file:delete"))],
)
async def batch_delete(
    ids: list[int],
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """批量删除文件"""
    identity = access_identity(_current_user)
    if identity.tenant != tenant:
        raise authorization_http._order_error()
    await authorization_http.release_request_reads(db)
    count = await authorization_http.get_authorization_runtime().upload.delete(
        identifiers=ids,
        batch=True,
        tenant=tenant,
        actor_user_version=identity.user_version,
    )
    return ResponseModel.success(msg=f"成功删除 {count} 个文件")
