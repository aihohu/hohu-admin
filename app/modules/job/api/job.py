import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import ResponseModel
from app.core.scheduler import notify_job_changed, notify_manual_trigger
from app.core.tenant import TenantContext
from app.db.authorization_http import get_authorization_runtime, release_request_reads
from app.db.session import get_db
from app.modules.auth.service import access_identity, get_current_tenant_context
from app.modules.job.schemas.control_job import (
    JobCreateForm,
    JobMetadataForm,
    JobScheduleWrite,
)
from app.modules.job.schemas.job import JobQuery
from app.modules.system.models.user import User
from app.modules.system.schemas.parameter_form import ParameterFormQuery
from app.modules.system.schemas.parameter_projection import (
    ParameterPage,
    ProjectedJobOut,
)

logger = logging.getLogger(__name__)
router = APIRouter()


async def _safe_publish(coro, label: str) -> bool:
    """发布调度器事件；Redis 抽风时只记日志，不影响 HTTP 响应。

    commit 已经发生，业务数据已落库；如果因为 Redis 抖动让接口 500，
    用户重试反而会触发重复创建（job_key 唯一约束会拦下，但 UX 差）。
    调度器进程下次启动 / 下次成功的 notify 会重新对齐状态。
    """
    try:
        async with asyncio.timeout(1):
            return await coro is True
    except Exception:
        logger.warning("调度器事件发布失败（%s）", label, exc_info=True)
        return False


async def _planned(db, actor, tenant, operation, **request):
    version = access_identity(actor).user_version
    await release_request_reads(db)
    return await get_authorization_runtime().job.execute(
        tenant=tenant,
        actor_user_version=version,
        operation=operation,
        http_protocol=True,
        **request,
    )


async def _read(db, actor, tenant, operation, query=None):
    version = access_identity(actor).user_version
    await release_request_reads(db)
    return await get_authorization_runtime().parameter_read(
        tenant=tenant, actor_user_version=version, operation=operation, query=query
    )


@router.get("/form")
async def get_form(
    query: Annotated[ParameterFormQuery, Query()],
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(data=await _read(db, actor, tenant, "job.form", query))


@router.get(
    "/list",
    response_model=ResponseModel[ParameterPage[ProjectedJobOut]],
    summary="获取定时任务列表",
    dependencies=[Depends(require_permissions("system:job:list"))],
)
async def get_list(
    query: JobQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    page_data = await _read(db, _current_user, tenant, "job.list", query)
    return ResponseModel.success(data=page_data)


@router.get(
    "/registered",
    response_model=ResponseModel[list],
    summary="获取已注册任务列表",
    dependencies=[Depends(require_permissions("system:job:list"))],
)
async def get_registered(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    tasks = await _read(db, _current_user, tenant, "job.registered")
    return ResponseModel.success(data=tasks)


@router.post(
    "/add",
    summary="创建定时任务",
    dependencies=[Depends(require_permissions("system:job:add"))],
)
async def add(
    data: JobCreateForm,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    job = await _planned(
        db,
        _current_user,
        tenant,
        "create",
        payload=data.model_dump(exclude={"form_receipt"}),
        protocol_receipt=data.form_receipt,
    )
    await _safe_publish(notify_job_changed(), "job_changed")
    # 创建即启用且要求立即执行：额外发一条 manual_trigger
    delivery = None
    if "dispatch" in job:
        confirmed = await _safe_publish(
            notify_manual_trigger(job["dispatch"]), "manual_trigger"
        )
        delivery = {"notificationStatus": "notified" if confirmed else "unconfirmed"}
    return ResponseModel.success(msg="创建成功", data=delivery)


@router.put(
    "/update",
    summary="更新定时任务",
    dependencies=[Depends(require_permissions("system:job:edit"))],
)
async def update(
    data: JobMetadataForm,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    await _planned(
        db,
        _current_user,
        tenant,
        "update",
        payload=data.model_dump(exclude_unset=True, exclude={"form_receipt"}),
        protocol_receipt=data.form_receipt,
    )
    await _safe_publish(notify_job_changed(), "job_changed")
    return ResponseModel.success(msg="更新成功")


@router.put("/schedule", dependencies=[Depends(require_permissions("system:job:edit"))])
async def update_schedule(
    data: JobScheduleWrite,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    await _planned(db, actor, tenant, "schedule", payload=data)
    await _safe_publish(notify_job_changed(), "job_changed")
    return ResponseModel.success(msg="调度配置更新成功")


@router.put(
    "/status",
    summary="启用/停用定时任务",
    dependencies=[Depends(require_permissions("system:job:edit"))],
)
async def update_status(
    jobId: int,
    status: str,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    job = await _planned(
        db, _current_user, tenant, "status", payload={"job_id": jobId, "status": status}
    )
    await _safe_publish(notify_job_changed(), "job_changed")
    # 启用动作 + run_on_enable：额外触发一次立即执行
    delivery = None
    if "dispatch" in job:
        confirmed = await _safe_publish(
            notify_manual_trigger(job["dispatch"]), "manual_trigger"
        )
        delivery = {"notificationStatus": "notified" if confirmed else "unconfirmed"}
    return ResponseModel.success(msg="状态更新成功", data=delivery)


@router.delete(
    "/{jobId}",
    summary="删除定时任务",
    dependencies=[Depends(require_permissions("system:job:delete"))],
)
async def delete(
    jobId: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    await _planned(db, _current_user, tenant, "delete", ids=(jobId,))
    await _safe_publish(notify_job_changed(), "job_changed")
    return ResponseModel.success(msg="删除成功")


@router.post(
    "/batch-delete",
    summary="批量删除定时任务",
    dependencies=[Depends(require_permissions("system:job:batch-delete"))],
)
async def batch_delete(
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    count = await _planned(db, _current_user, tenant, "batch-delete", ids=ids)
    await _safe_publish(notify_job_changed(), "job_changed")
    return ResponseModel.success(msg=f"已删除 {count} 个任务")


@router.post(
    "/run/{jobId}",
    summary="手动触发任务",
    dependencies=[Depends(require_permissions("system:job:run"))],
)
async def run_now(
    jobId: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    notice = await _planned(db, _current_user, tenant, "run", ids=(jobId,))
    confirmed = await _safe_publish(
        notify_manual_trigger(notice["dispatch"]), "manual_trigger"
    )
    return ResponseModel.success(
        msg="已发送运行通知" if confirmed else "运行通知未确认",
        data={"notificationStatus": "notified" if confirmed else "unconfirmed"},
    )
