from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.base_response import PageResult, ResponseModel
from app.core.scheduler import notify_job_changed, notify_manual_trigger
from app.db.session import get_db
from app.modules.job.schemas.job import (
    JobCreate,
    JobOut,
    JobQuery,
    JobUpdate,
)
from app.modules.job.service.job_service import job_service
from app.modules.job.task_registry import list_registered_tasks
from app.modules.system.models.user import User

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[JobOut]],
    summary="获取定时任务列表",
)
async def get_list(
    query: JobQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    page_data = await job_service.get_list(db, query)
    return ResponseModel.success(data=page_data)


@router.get(
    "/registered", response_model=ResponseModel[list], summary="获取已注册任务列表"
)
async def get_registered(
    _current_user: User = Depends(get_current_user),
):
    tasks = list_registered_tasks()
    return ResponseModel.success(data=tasks)


@router.post("/add", summary="创建定时任务")
async def add(
    data: JobCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    await job_service.create(db, data, _current_user.user_name)
    await db.commit()
    await notify_job_changed()
    return ResponseModel.success(msg="创建成功")


@router.put("/update", summary="更新定时任务")
async def update(
    data: JobUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    await job_service.update(db, data, _current_user.user_name)
    await db.commit()
    await notify_job_changed()
    return ResponseModel.success(msg="更新成功")


@router.put("/status", summary="启用/停用定时任务")
async def update_status(
    jobId: int,
    status: str,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    await job_service.update_status(db, jobId, status)
    await db.commit()
    await notify_job_changed()
    return ResponseModel.success(msg="状态更新成功")


@router.delete("/{jobId}", summary="删除定时任务")
async def delete(
    jobId: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    await job_service.delete(db, jobId)
    await db.commit()
    await notify_job_changed()
    return ResponseModel.success(msg="删除成功")


@router.post("/batch-delete", summary="批量删除定时任务")
async def batch_delete(
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    count = await job_service.batch_delete(db, ids)
    await db.commit()
    await notify_job_changed()
    return ResponseModel.success(msg=f"已删除 {count} 个任务")


@router.post("/run/{jobId}", summary="手动触发任务")
async def run_now(
    jobId: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    await job_service.run_now(db, jobId)
    await notify_manual_trigger(jobId)
    return ResponseModel.success(msg="已触发执行")
