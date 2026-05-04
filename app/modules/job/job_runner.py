import json
import logging
import traceback
from datetime import datetime

from sqlalchemy import select

from app.constants import STATUS_ENABLED
from app.db.session import AsyncSessionLocal
from app.modules.job.models.job import SysJob, SysJobLog
from app.modules.job.task_registry import get_task_function

logger = logging.getLogger(__name__)

# 日志状态常量
LOG_STATUS_RUNNING = "3"
LOG_STATUS_SUCCESS = "1"
LOG_STATUS_FAILED = "2"


async def execute_job(job_id: int) -> None:
    """由 APScheduler 调度的入口函数，负责执行任务并记录日志。"""
    await _do_execute(job_id)


async def run_job_manual(job_id: int) -> None:
    """手动触发执行，跳过状态检查。"""
    await _do_execute(job_id, skip_status_check=True)


async def _do_execute(job_id: int, *, skip_status_check: bool = False) -> None:
    """执行任务并记录日志。"""
    log: SysJobLog | None = None
    async with AsyncSessionLocal() as db:
        try:
            job: SysJob | None = await db.get(SysJob, job_id)
            if not job:
                return
            if not skip_status_check and job.status != STATUS_ENABLED:
                return

            # 并发检查：如果不允许并发且当前有执行中的记录，则跳过
            if job.concurrent == "2":
                running_stmt = select(SysJobLog).where(
                    SysJobLog.job_id == job_id,
                    SysJobLog.status == LOG_STATUS_RUNNING,
                )
                result = await db.execute(running_stmt)
                if result.scalars().first():
                    return

            # 写入执行日志（执行中）
            log = SysJobLog(
                job_id=job.job_id,
                job_name=job.job_name,
                job_key=job.job_key,
                status=LOG_STATUS_RUNNING,
                start_time=datetime.now(),
            )
            db.add(log)
            await db.commit()
            await db.refresh(log)

            # 执行任务
            await _run_task(db, job, log)
        except Exception:
            logger.exception("任务执行异常: job_id=%s", job_id)
            # 如果日志已写入 running 但后续异常，尝试标记为 failed
            if log:
                try:
                    log.status = LOG_STATUS_FAILED
                    log.error_msg = traceback.format_exc()
                    log.end_time = datetime.now()
                    if log.start_time:
                        log.duration = int(
                            (log.end_time - log.start_time).total_seconds() * 1000
                        )
                    await db.commit()
                except Exception:
                    logger.exception("标记日志失败: job_id=%s", job_id)
                    await db.rollback()
            else:
                await db.rollback()


async def _run_task(db: AsyncSessionLocal, job: SysJob, log: SysJobLog) -> None:
    """执行任务函数，成功标 SUCCESS，失败标 FAILED。"""
    func = get_task_function(job.job_key)
    if func is None:
        log.status = LOG_STATUS_FAILED
        log.error_msg = f"任务函数 '{job.job_key}' 未注册"
        log.end_time = datetime.now()
        log.duration = int((log.end_time - log.start_time).total_seconds() * 1000)
        await db.commit()
        return

    try:
        args = job.job_args
        kwargs = {}
        if args:
            kwargs = {"args": json.loads(args)}

        await func(**kwargs)

        log.status = LOG_STATUS_SUCCESS
        log.end_time = datetime.now()
        log.duration = int((log.end_time - log.start_time).total_seconds() * 1000)
        await db.commit()
    except Exception:
        log.status = LOG_STATUS_FAILED
        log.error_msg = traceback.format_exc()
        log.end_time = datetime.now()
        log.duration = int((log.end_time - log.start_time).total_seconds() * 1000)
        await db.commit()
