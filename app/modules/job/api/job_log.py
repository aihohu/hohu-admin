from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.tenant import TenantContext
from app.db.session import get_db
from app.modules.auth.service import get_current_tenant_context
from app.modules.job.schemas.job import JobLogOut, JobLogQuery
from app.modules.job.service.job_log_service import job_log_service
from app.modules.system.models.user import User

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[JobLogOut]],
    summary="获取任务日志列表",
    dependencies=[Depends(require_permissions("system:job-log:list"))],
)
async def get_list(
    query: JobLogQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    page_data = await job_log_service.get_list(db, query, tenant=tenant)
    return ResponseModel.success(data=page_data)


@router.delete(
    "/clean",
    summary="清理任务日志",
    dependencies=[Depends(require_permissions("system:job-log:clean"))],
)
async def clean(
    days: int = Query(30, ge=1, description="清理多少天前的日志"),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    count = await job_log_service.clean(db, days, tenant=tenant)
    await db.commit()
    return ResponseModel.success(msg=f"已清理 {count} 条日志")


@router.post(
    "/batch-delete",
    summary="批量删除任务日志",
    dependencies=[Depends(require_permissions("system:job-log:batch-delete"))],
)
async def batch_delete(
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    count = await job_log_service.batch_delete(db, ids, tenant=tenant)
    await db.commit()
    return ResponseModel.success(msg=f"已删除 {count} 条日志")
