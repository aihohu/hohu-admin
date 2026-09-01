import logging

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED
from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.scheduler import notify_job_changed, notify_manual_trigger
from app.core.tenant import TenantContext
from app.db.session import get_db
from app.modules.auth.service import get_current_tenant_context
from app.modules.job.schemas.job import (
    JobCreate,
    JobOut,
    JobQuery,
    JobUpdate,
)
from app.modules.job.service.job_service import job_service
from app.modules.job.task_registry import TaskScope, list_registered_tasks
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()


async def _safe_publish(coro, label: str) -> None:
    """发布调度器事件；Redis 抽风时只记日志，不影响 HTTP 响应。

    commit 已经发生，业务数据已落库；如果因为 Redis 抖动让接口 500，
    用户重试反而会触发重复创建（job_key 唯一约束会拦下，但 UX 差）。
    调度器进程下次启动 / 下次成功的 notify 会重新对齐状态。
    """
    try:
        await coro
    except Exception:
        logger.warning("调度器事件发布失败（%s）", label, exc_info=True)


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[JobOut]],
    summary="获取定时任务列表",
    dependencies=[Depends(require_permissions("system:job:list"))],
)
async def get_list(
    query: JobQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    page_data = await job_service.get_list(db, query, tenant=tenant)
    return ResponseModel.success(data=page_data)


@router.get(
    "/registered",
    response_model=ResponseModel[list],
    summary="获取已注册任务列表",
    dependencies=[Depends(require_permissions("system:job:list"))],
)
async def get_registered(
    _current_user: User = Depends(get_current_user),
):
    tasks = list_registered_tasks(scope=TaskScope.TENANT)
    return ResponseModel.success(data=tasks)


@router.post(
    "/add",
    summary="创建定时任务",
    dependencies=[Depends(require_permissions("system:job:add"))],
)
async def add(
    data: JobCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    job = await job_service.create(db, data, _current_user.user_name, tenant=tenant)
    await db.commit()
    await _safe_publish(notify_job_changed(), "job_changed")
    # 创建即启用且要求立即执行：额外发一条 manual_trigger
    if data.status == STATUS_ENABLED and data.run_on_enable:
        await _safe_publish(notify_manual_trigger(tenant, job.job_id), "manual_trigger")
    return ResponseModel.success(msg="创建成功")


@router.put(
    "/update",
    summary="更新定时任务",
    dependencies=[Depends(require_permissions("system:job:edit"))],
)
async def update(
    data: JobUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    await job_service.update(db, data, _current_user.user_name, tenant=tenant)
    await db.commit()
    await _safe_publish(notify_job_changed(), "job_changed")
    return ResponseModel.success(msg="更新成功")


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
    job = await job_service.update_status(db, jobId, status, tenant=tenant)
    await db.commit()
    await _safe_publish(notify_job_changed(), "job_changed")
    # 启用动作 + run_on_enable：额外触发一次立即执行
    if status == STATUS_ENABLED and job.run_on_enable:
        await _safe_publish(notify_manual_trigger(tenant, job.job_id), "manual_trigger")
    return ResponseModel.success(msg="状态更新成功")


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
    await job_service.delete(db, jobId, tenant=tenant)
    await db.commit()
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
    count = await job_service.batch_delete(db, ids, tenant=tenant)
    await db.commit()
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
    await job_service.run_now(db, jobId, tenant=tenant)
    await _safe_publish(notify_manual_trigger(tenant, jobId), "manual_trigger")
    return ResponseModel.success(msg="已触发执行")
