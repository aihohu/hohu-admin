"""调度器独立进程入口。

生产部署时与 web 进程分离运行：

    python -m app.scheduler_worker

该进程仅承担：
1. 从 DB 加载启用的定时任务到 APScheduler
2. 订阅 Redis 控制频道（scheduler:job_changed / scheduler:manual_trigger）
3. 收到 job_changed 时重新对齐 DB；收到 manual_trigger 时立即执行

web 进程（APP_ROLE=api）通过 Redis pub/sub 通知本进程。
"""

import asyncio
import logging
import signal
from contextlib import AsyncExitStack

from anyio import CancelScope

import app.tasks  # noqa: F401  # 触发 @register_task 装饰器
from app.core.config import settings
from app.core.process_admission import ProcessAdmission
from app.core.redis import close_redis
from app.core.scheduler import scheduler_manager
from app.core.tenant import PlatformContext
from app.core.write_admission import require_open, write_admission
from app.db.session import AsyncSessionLocal
from app.modules.job.job_runner import RUNNER_ID

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scheduler_worker")


async def _run() -> None:
    require_open()
    platform = PlatformContext(
        actor_principal_id=0,
        actor_name="scheduler-worker",
        principal_type="service",
        permissions=frozenset({"platform:job:operate"}),
        reason="scheduler worker tenant enumeration",
        ticket_id="SYSTEM-SCHEDULER",
        correlation_id=f"scheduler:{RUNNER_ID}",
    )
    async with AsyncSessionLocal() as db:
        await scheduler_manager.reload_jobs(db, platform=platform)
    await scheduler_manager.start_with_pubsub(platform=platform)
    logger.info("Scheduler worker 已就绪")


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    process = ProcessAdmission(
        write_admission,
        role="scheduler",
        build_sha=settings.RELEASE_BUILD_SHA,
        stop_callbacks=(scheduler_manager.stop,),
    )

    def _request_stop(*_args: object) -> None:
        # Wake the selector even after all scheduler/background IO has drained.
        # Event.set alone does not wake an idle loop from a signal handler.
        loop.call_soon_threadsafe(stop_event.set)

    # SIGINT 在 Windows/Linux 都可用；SIGTERM 仅 POSIX，Windows 下注册不会报错但无效
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_stop)
        except (OSError, ValueError):
            pass

    cleanup = AsyncExitStack()
    cleanup.push_async_callback(close_redis)
    cleanup.push_async_callback(process.stop)
    cleanup.push_async_callback(scheduler_manager.stop)
    try:
        await process.start()
        await _run()
        await stop_event.wait()
    finally:
        logger.info("Scheduler worker 收到退出信号，正在关闭")
        with CancelScope(shield=True):
            async with asyncio.timeout(75):
                await cleanup.aclose()
        logger.info("Scheduler worker 已退出")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
