import asyncio
import json
import logging
import traceback
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


async def _run_task(db: AsyncSession, job: SysJob, log: SysJobLog) -> None:
    """执行任务函数，支持单次超时和失败重试。

    超时：`job.timeout_seconds` 为非 None 时用 `asyncio.wait_for` 包裹。
    重试：`job.max_retries` > 0 时失败后重试，最多 max_retries 次。
    重试间策略：
    1. rollback 当前 session，丢弃失败尝试可能留下的脏数据 / 失活状态
    2. 指数退避 sleep（1s, 2s, 4s, ..., 上限 30s），避免对外部服务形成重试风暴
    成功任一次即标 SUCCESS；所有重试均失败才标 FAILED，错误信息含最后一次异常。
    """
    func = get_task_function(job.job_key)
    if func is None:
        log.status = LOG_STATUS_FAILED
        log.error_msg = f"任务函数 '{job.job_key}' 未注册"
        log.end_time = datetime.now()
        log.duration = int((log.end_time - log.start_time).total_seconds() * 1000)
        await db.commit()
        return

    args = job.job_args
    kwargs = {}
    if args:
        kwargs = {"args": json.loads(args)}

    timeout = job.timeout_seconds
    max_retries = max(0, job.max_retries or 0)
    total_attempts = max_retries + 1

    last_error: str = ""
    last_was_timeout = False
    for attempt in range(1, total_attempts + 1):
        try:
            if timeout:
                await asyncio.wait_for(func(**kwargs), timeout=timeout)
            else:
                await func(**kwargs)
            log.status = LOG_STATUS_SUCCESS
            log.attempt_count = attempt
            log.end_time = datetime.now()
            log.duration = int((log.end_time - log.start_time).total_seconds() * 1000)
            await db.commit()
            return
        except TimeoutError:
            # 必须在 Exception 之前捕获；wait_for 超时单独标记
            last_error = traceback.format_exc()
            last_was_timeout = True
        except Exception:
            last_error = traceback.format_exc()
            last_was_timeout = False

        if attempt >= total_attempts:
            logger.error(
                "任务 %d 次执行均失败（最后失败类型：%s）: job_id=%s",
                total_attempts,
                "超时" if last_was_timeout else "异常",
                job.job_id,
            )
            break

        # 1. rollback：失败尝试可能让 session 进入失活状态或留下未提交脏数据，
        #    不 rollback 会污染下一次 retry（"This Session's transaction has been rolled back"）
        await db.rollback()
        # 2. 指数退避：1s, 2s, 4s, 8s, 16s, ..., 上限 30s
        delay = min(2 ** (attempt - 1), 30)
        logger.warning(
            "任务第 %d/%d 次执行失败（%s），%ss 后重试: job_id=%s",
            attempt,
            total_attempts,
            "超时" if last_was_timeout else "异常",
            delay,
            job.job_id,
        )
        await asyncio.sleep(delay)
        continue

    # 所有重试均失败
    failure_type = "执行超时" if last_was_timeout else "执行异常"
    log.status = LOG_STATUS_FAILED
    log.error_msg = (
        f"连续 {total_attempts} 次执行失败（最后失败类型：{failure_type}，"
        f"超时配置={timeout or '不限'}s），最后一次异常:\n{last_error}"
    )
    log.attempt_count = total_attempts
    log.end_time = datetime.now()
    log.duration = int((log.end_time - log.start_time).total_seconds() * 1000)
    await db.commit()
