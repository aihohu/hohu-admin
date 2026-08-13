from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED
from app.core.exceptions import (
    BusinessRuleException,
    DuplicateException,
    NotFoundException,
)
from app.core.scheduler import build_trigger, validate_trigger_config
from app.modules.job.models.job import SysJob, SysJobLog
from app.modules.job.schemas.job import JobAiUpdate, JobCreate, JobQuery, JobUpdate
from app.modules.job.task_registry import get_task_function
from app.utils.pagination import build_filters, paginate


class JobService:
    """定时任务配置业务逻辑服务"""

    async def get_list(self, db: AsyncSession, query: JobQuery):
        """获取定时任务分页列表。

        会为每个启用任务计算 next_run_time（运行时字段，不落库）。
        停用任务或 trigger 配置异常的，next_run_time 为 None。
        """
        field_mapping = {
            "job_name": ("job_name", "contains"),
            "job_key": ("job_key", "contains"),
            "status": ("status", "=="),
        }
        filters = build_filters(SysJob, field_mapping, **query.model_dump())
        page_data = await paginate(
            db=db,
            model=SysJob,
            query_params=query,
            filters=filters,
            order_by=SysJob.create_time.desc(),
        )
        # 计算下次执行时间（仅在列表展示用，不影响调度）
        now = datetime.now()
        for job in page_data.records:
            job.next_run_time = self._compute_next_run_time(job, now)
        return page_data

    @staticmethod
    def _compute_next_run_time(job: SysJob, now: datetime):
        """根据 trigger 配置独立计算下次执行时间，不依赖 scheduler 实例。"""
        if job.status != STATUS_ENABLED:
            return None
        try:
            trigger = build_trigger(job)
            return trigger.get_next_fire_time(None, now)
        except Exception:
            # trigger 配置异常（如 cron 表达式错误），交给具体触发时报错
            return None

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

        return job

    async def update_status(self, db: AsyncSession, job_id: int, status: str) -> SysJob:
        """启用/停用任务。"""
        job = await self.get_by_id(db, job_id)
        job.status = status
        return job

    async def update_for_ai(
        self, db: AsyncSession, data: JobAiUpdate, current_user: str | None = None
    ) -> SysJob:
        """AI 入口更新，强制使用 ``JobAiUpdate`` 字段白名单。

        即使 AI tool 漏传 job_key / run_on_enable 等危险字段，Pydantic 在反序列化
        阶段就丢弃（不报错，但字段不进 update_data）。安全边界由 schema 定义，
        不依赖调用方纪律。

        Args:
            data: JobAiUpdate（白名单 schema，禁止字段已在 schema 层排除）
            current_user: AI 用户标识，写入 update_by
        """
        # 把白名单字段映射回 JobUpdate（复用 update 的 trigger 校验逻辑）
        job_update = JobUpdate(**data.model_dump(exclude_unset=True))
        return await self.update(db, job_update, current_user=current_user)

    async def delete(self, db: AsyncSession, job_id: int) -> None:
        """删除任务（仅停用状态可删），同时删除关联日志。"""
        job = await self.get_by_id(db, job_id)
        if job.status == STATUS_ENABLED:
            raise BusinessRuleException("请先停用任务再删除")

        await db.execute(delete(SysJobLog).where(SysJobLog.job_id == job_id))
        await db.delete(job)

    async def batch_delete(self, db: AsyncSession, ids: list[int]) -> int:
        """批量删除任务（仅停用状态可删），同时删除关联日志。"""
        count = 0
        for job_id in ids:
            job = await db.get(SysJob, job_id)
            if job and job.status != STATUS_ENABLED:
                await db.execute(delete(SysJobLog).where(SysJobLog.job_id == job_id))
                await db.delete(job)
                count += 1
        return count

    async def run_now(self, db: AsyncSession, job_id: int) -> SysJob:
        """手动触发任务立即执行。

        刻意不做 status 校验：手动触发是调试/应急通道，停用的任务也应能触发。
        调度器侧的 `run_job_manual` 同样使用 `skip_status_check=True` 与此对齐。
        实际触发动作由 API 层在 commit 之后通过 `notify_manual_trigger`
        发送给调度器进程。
        """
        return await self.get_by_id(db, job_id)


job_service = JobService()
