from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED
from app.core.exceptions import (
    BusinessRuleException,
    DuplicateException,
    NotFoundException,
)
from app.core.scheduler import scheduler_manager, validate_trigger_config
from app.modules.job.models.job import SysJob, SysJobLog
from app.modules.job.schemas.job import JobCreate, JobQuery, JobUpdate
from app.modules.job.task_registry import get_task_function
from app.utils.pagination import build_filters, paginate


class JobService:
    """定时任务配置业务逻辑服务"""

    async def get_list(self, db: AsyncSession, query: JobQuery):
        """获取定时任务分页列表。"""
        field_mapping = {
            "job_name": ("job_name", "contains"),
            "job_key": ("job_key", "contains"),
            "status": ("status", "=="),
        }
        filters = build_filters(SysJob, field_mapping, **query.model_dump())
        return await paginate(
            db=db,
            model=SysJob,
            query_params=query,
            filters=filters,
            order_by=SysJob.create_time.desc(),
        )

    async def get_by_id(self, db: AsyncSession, job_id: int) -> SysJob:
        """根据 ID 获取任务，不存在则抛异常。"""
        job = await db.get(SysJob, job_id)
        if not job:
            raise NotFoundException("定时任务")
        return job

    async def create(
        self, db: AsyncSession, data: JobCreate, current_user: str | None = None
    ) -> SysJob:
        """创建定时任务。"""
        # 校验 job_key 在 Registry 中存在
        if get_task_function(data.job_key) is None:
            raise BusinessRuleException(f"任务标识 '{data.job_key}' 未注册")

        # 校验调度配置合法
        try:
            validate_trigger_config(
                data.trigger_type,
                data.cron_expression,
                data.interval_value,
                data.interval_unit,
            )
        except ValueError as e:
            raise BusinessRuleException(str(e)) from e

        # 校验 job_key 唯一
        existing = await db.execute(
            select(SysJob).where(SysJob.job_key == data.job_key)
        )
        if existing.scalars().first():
            raise DuplicateException("任务标识", data.job_key)

        dump = data.model_dump()
        if current_user:
            dump["create_by"] = current_user
            dump["update_by"] = current_user

        job = SysJob(**dump)
        db.add(job)
        await db.flush()

        # 如果启用，注册到调度器
        if data.status == STATUS_ENABLED:
            await db.refresh(job)
            scheduler_manager.add_job(job)

        return job

    async def update(
        self, db: AsyncSession, data: JobUpdate, current_user: str | None = None
    ) -> SysJob:
        """更新定时任务。"""
        job = await self.get_by_id(db, data.job_id)

        update_data = data.model_dump(exclude_unset=True, exclude={"job_id"})
        if current_user:
            update_data["update_by"] = current_user

        # 如果更新了调度配置，校验合法性
        trigger_fields = {
            "trigger_type",
            "cron_expression",
            "interval_value",
            "interval_unit",
        }
        if trigger_fields & update_data.keys():
            trigger_type = update_data.get("trigger_type", job.trigger_type)
            cron_expression = update_data.get("cron_expression", job.cron_expression)
            interval_value = update_data.get("interval_value", job.interval_value)
            interval_unit = update_data.get("interval_unit", job.interval_unit)
            try:
                validate_trigger_config(
                    trigger_type, cron_expression, interval_value, interval_unit
                )
            except ValueError as e:
                raise BusinessRuleException(str(e)) from e

        for field, value in update_data.items():
            setattr(job, field, value)

        await db.flush()
        await db.refresh(job)

        # 同步调度器
        scheduler_manager.remove_job(job.job_id)
        if job.status == STATUS_ENABLED:
            scheduler_manager.add_job(job)

        return job

    async def update_status(self, db: AsyncSession, job_id: int, status: str) -> SysJob:
        """启用/停用任务。"""
        job = await self.get_by_id(db, job_id)
        job.status = status

        if status == STATUS_ENABLED:
            scheduler_manager.add_job(job)
        else:
            scheduler_manager.remove_job(job.job_id)

        return job

    async def delete(self, db: AsyncSession, job_id: int) -> None:
        """删除任务（仅停用状态可删），同时删除关联日志。"""
        job = await self.get_by_id(db, job_id)
        if job.status == STATUS_ENABLED:
            raise BusinessRuleException("请先停用任务再删除")

        await db.execute(delete(SysJobLog).where(SysJobLog.job_id == job_id))
        await db.delete(job)
        # DB 操作成功后再移除调度器（调度器操作不可回滚）
        scheduler_manager.remove_job(job.job_id)

    async def batch_delete(self, db: AsyncSession, ids: list[int]) -> int:
        """批量删除任务（仅停用状态可删），同时删除关联日志。"""
        count = 0
        removed_job_ids: list[int] = []
        for job_id in ids:
            job = await db.get(SysJob, job_id)
            if job and job.status != STATUS_ENABLED:
                await db.execute(delete(SysJobLog).where(SysJobLog.job_id == job_id))
                await db.delete(job)
                removed_job_ids.append(job.job_id)
                count += 1
        # DB 操作全部成功后再移除调度器（调度器操作不可回滚）
        for job_id in removed_job_ids:
            scheduler_manager.remove_job(job_id)
        return count

    async def run_now(self, db: AsyncSession, job_id: int) -> SysJob:
        """手动触发任务立即执行。"""
        job = await self.get_by_id(db, job_id)
        scheduler_manager.run_now(job.job_id)
        return job


job_service = JobService()
