from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import is_super_admin, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.tenant import resolve_tenant_id
from app.db.session import get_db
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User
from app.modules.system.schemas.file import FileOut, FileQuery
from app.modules.system.service.file_service import file_service

router = APIRouter()


@router.post(
    "/upload",
    response_model=ResponseModel[FileOut],
    summary="单文件上传",
    description="上传单个文件，可选关联业务类型和业务ID",
)
async def upload(
    file: Annotated[UploadFile, File(description="上传的文件")],
    business_type: str | None = Form(None, description="业务类型(如product、avatar)"),
    business_id: int | None = Form(None, description="业务记录ID"),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """上传单个文件"""
    file_record = await file_service.upload(
        db,
        file,
        current_user_name=_current_user.user_name,
        business_type=business_type,
        business_id=business_id,
        owner_user_id=_current_user.user_id,
        tenant_id=resolve_tenant_id(_current_user),
    )
    await db.commit()
    await db.refresh(file_record)
    return ResponseModel.success(data=file_record, msg="文件上传成功")


@router.post(
    "/batch-upload",
    response_model=ResponseModel[list[FileOut]],
    summary="多文件上传",
    description="批量上传多个文件, 可选关联业务类型和业务ID",
)
async def batch_upload(
    files: Annotated[list[UploadFile], File(description="上传的文件列表")],
    business_type: str | None = Form(None, description="业务类型"),
    business_id: int | None = Form(None, description="业务记录ID"),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """批量上传文件"""
    file_records = await file_service.batch_upload(
        db,
        files,
        current_user_name=_current_user.user_name,
        business_type=business_type,
        business_id=business_id,
        owner_user_id=_current_user.user_id,
        tenant_id=resolve_tenant_id(_current_user),
    )
    await db.commit()
    for record in file_records:
        await db.refresh(record)
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
):
    """获取文件分页列表"""
    page_data = await file_service.get_list(
        db,
        query,
        tenant_id=resolve_tenant_id(_current_user),
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
):
    """获取文件详情"""
    admin = is_super_admin(_current_user)
    file_record = await file_service.get_by_id(
        db,
        file_id,
        tenant_id=resolve_tenant_id(_current_user),
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
):
    """删除文件（ownership 检查在 service）"""
    await file_service.delete(
        db,
        file_id,
        current_user=current_user,
        is_admin=is_super_admin(current_user),
        tenant_id=resolve_tenant_id(current_user),
    )
    await db.commit()
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
):
    """批量删除文件"""
    count = await file_service.batch_delete(
        db,
        ids,
        current_user=_current_user,
        is_admin=True,
        tenant_id=resolve_tenant_id(_current_user),
    )
    await db.commit()
    return ResponseModel.success(msg=f"成功删除 {count} 个文件")
