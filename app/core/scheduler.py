import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.constants import STATUS_ENABLED
from app.modules.job.job_runner import execute_job, run_job_manual
from app.modules.job.models.job import SysJob

logger = logging.getLogger(__name__)


def build_trigger(job: SysJob):
    """根据任务配置构建 APScheduler trigger，兼容 5/6 字段 cron 和 interval。"""
    if job.trigger_type == "interval":
        kwargs = {}
        if job.interval_value and job.interval_unit:
            kwargs[job.interval_unit] = job.interval_value
        return IntervalTrigger(**kwargs)

    # cron 模式
    expr = job.cron_expression
    if not expr:
        msg = "cron 模式必须提供 cron 表达式"
        raise ValueError(msg)

    parts = expr.strip().split()
    if len(parts) == 5:
        return CronTrigger.from_crontab(expr)
    if len(parts) == 6:
        # 6 字段：秒 分 时 日 月 周
        return CronTrigger(
            second=parts[0],
            minute=parts[1],
            hour=parts[2],
            day=parts[3],
            month=parts[4],
            day_of_week=parts[5],
        )

    msg = f"cron 表达式字段数不合法（期望 5 或 6，实际 {len(parts)}）: {expr}"
    raise ValueError(msg)


def validate_trigger_config(
    trigger_type: str,
    cron_expression: str | None,
    interval_value: int | None,
    interval_unit: str | None,
):
    """校验调度配置是否合法，不抛异常则合法。"""
    if trigger_type == "interval":
        if not interval_value or not interval_unit:
            msg = "interval 模式必须提供间隔值和间隔单位"
            raise ValueError(msg)
        if interval_unit not in ("seconds", "minutes", "hours", "days"):
            msg = f"间隔单位不合法: {interval_unit}"
            raise ValueError(msg)
    else:
        if not cron_expression:
            msg = "cron 模式必须提供 cron 表达式"
            raise ValueError(msg)
        parts = cron_expression.strip().split()
        if len(parts) not in (5, 6):
            msg = f"cron 表达式字段数不合法（期望 5 或 6，实际 {len(parts)}）"
            raise ValueError(msg)
        # 用实际解析验证
        if len(parts) == 5:
            CronTrigger.from_crontab(cron_expression)
        else:
            CronTrigger(
                second=parts[0],
                minute=parts[1],
                hour=parts[2],
                day=parts[3],
                month=parts[4],
                day_of_week=parts[5],
            )


class SchedulerManager:
    """封装 APScheduler，管理定时任务的启动、停止和调度。"""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        """启动调度器。"""
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("APScheduler 已启动")

    def shutdown(self, wait: bool = True) -> None:
        """关闭调度器。"""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            logger.info("APScheduler 已关闭")

    def add_job(self, job: SysJob) -> None:
        """将一个 SysJob 添加到调度器。"""
        if job.status != STATUS_ENABLED:
            return
        trigger = build_trigger(job)
        self._scheduler.add_job(
            execute_job,
            trigger=trigger,
            args=[job.job_id],
            id=f"job_{job.job_id}",
            replace_existing=True,
        )
        logger.info("已注册调度任务: %s (type=%s)", job.job_key, job.trigger_type)

    def remove_job(self, job_id: int) -> None:
        """从调度器移除一个任务。"""
        job_id_str = f"job_{job_id}"
        existing = self._scheduler.get_job(job_id_str)
        if existing:
            self._scheduler.remove_job(job_id_str)
            logger.info("已移除调度任务: job_id=%s", job_id)

    async def load_jobs_from_db(self, db) -> None:
        """从数据库加载所有启用的任务并注册到调度器。"""
        stmt = select(SysJob).where(SysJob.status == STATUS_ENABLED)
        result = await db.execute(stmt)
        jobs = result.scalars().all()
        for job in jobs:
            self.add_job(job)
        logger.info("从数据库加载了 %d 个调度任务", len(jobs))

    def run_now(self, job_id: int) -> None:
        """立即执行一次指定任务（不等待调度）。"""
        self._scheduler.add_job(
            run_job_manual,
            args=[job_id],
            id=f"manual_{job_id}",
            replace_existing=True,
        )
        logger.info("手动触发任务: job_id=%s", job_id)


# 全局单例
scheduler_manager = SchedulerManager()
