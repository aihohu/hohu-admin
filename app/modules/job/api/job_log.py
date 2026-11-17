from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import ResponseModel
from app.core.tenant import TenantContext
from app.db import authorization_http
from app.db.session import get_db
from app.modules.auth.service import access_identity, get_current_tenant_context
from app.modules.job.api.job import _planned
from app.modules.job.schemas.job import JobLogQuery
from app.modules.system.models.user import User
from app.modules.system.schemas.delivery_projection import DeliveryPage, ProjectedJobLog

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[DeliveryPage[ProjectedJobLog]],
    response_model_exclude_unset=True,
    summary="获取任务日志列表",
    dependencies=[Depends(require_permissions("system:job-log:list"))],
)
async def get_list(
    query: JobLogQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    identity = access_identity(_current_user)
    if identity.tenant != tenant:
        from app.db.authorization_plan_session import _order_error  # noqa: PLC0415

        raise _order_error()
    await authorization_http.release_request_reads(db)
    page_data = await authorization_http.get_authorization_runtime().delivery.logs(
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        kind="job",
        query=query,
    )
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
    count = await _planned(db, _current_user, tenant, "log-clean", days=days)
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
    count = await _planned(db, _current_user, tenant, "log-delete", ids=ids)
    return ResponseModel.success(msg=f"已删除 {count} 条日志")
