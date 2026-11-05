"""Claim task execution, release the database, then persist a bounded completion."""

import asyncio
import json
import logging
import traceback
from uuid import uuid4

from app.core.write_admission import admitted
from app.db.authorization_http import get_authorization_runtime
from app.modules.job.task_registry import get_task_function, is_tenant_task

logger = logging.getLogger(__name__)
LOG_STATUS_RUNNING = "3"
LOG_STATUS_SUCCESS = "1"
LOG_STATUS_FAILED = "2"
RUNNER_ID = uuid4().hex


async def execute_job(tenant_id: int, job_id: int) -> None:
    await _do_execute(tenant_id, job_id)


async def run_job_manual(tenant_id: int, job_id: int) -> None:
    await _do_execute(tenant_id, job_id, skip_status_check=True)


@admitted
async def _do_execute(
    tenant_id: int, job_id: int, *, skip_status_check: bool = False
) -> None:
    owner = get_authorization_runtime().job
    try:
        claim = await owner.claim(
            tenant_id=tenant_id,
            job_id=job_id,
            runner_id=RUNNER_ID,
            manual=skip_status_check,
        )
    except Exception:
        # A claim whose ACK is unknown must never dispatch the task.
        logger.exception("任务认领未确认: job_id=%s", job_id)
        return
    if claim is None:
        return
    try:
        status, error, attempts = await _run_task(claim)
    except asyncio.CancelledError:
        # Preserve RUNNING: cancellation is not proof of a failed side effect.
        raise
    except Exception:
        status, error, attempts = LOG_STATUS_FAILED, traceback.format_exc()[-65536:], 1
    try:
        await owner.finish(claim, status=status, error_msg=error, attempts=attempts)
    except Exception:
        # Completion failure never enters the business retry loop.
        logger.exception("任务完成日志未确认: job_id=%s", job_id)


async def _run_task(claim):
    """Only task failures use the configured retry policy; no session is held."""
    claim.require()
    job = claim.job
    func = get_task_function(job["job_key"]) if is_tenant_task(job["job_key"]) else None
    if func is None:
        return LOG_STATUS_FAILED, f"任务函数 '{job['job_key']}' 未注册", 1
    kwargs = {"args": json.loads(job["job_args"])} if job["job_args"] else {}
    timeout = job["timeout_seconds"]
    total = max(0, job["max_retries"] or 0) + 1
    last_error, last_timeout = "", False
    for attempt in range(1, total + 1):
        try:
            if timeout:
                await asyncio.wait_for(func(**kwargs), timeout=timeout)
            else:
                await func(**kwargs)
            return LOG_STATUS_SUCCESS, None, attempt
        except TimeoutError:
            last_error, last_timeout = traceback.format_exc(), True
        except Exception:
            last_error, last_timeout = traceback.format_exc(), False
        if attempt < total:
            await asyncio.sleep(min(2 ** (attempt - 1), 30))
    failure = "执行超时" if last_timeout else "执行异常"
    return (
        LOG_STATUS_FAILED,
        (
            f"连续 {total} 次执行失败（最后失败类型：{failure}，超时配置={timeout or '不限'}s），最后一次异常:\n{last_error}"
        )[-65536:],
        total,
    )
