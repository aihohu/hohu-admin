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

import app.tasks  # noqa: F401  # 触发 @register_task 装饰器
from app.core.redis import close_redis
from app.core.scheduler import scheduler_manager
from app.db.session import AsyncSessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scheduler_worker")


async def _run() -> None:
    async with AsyncSessionLocal() as db:
        await scheduler_manager.load_jobs_from_db(db)
    await scheduler_manager.start_with_pubsub()
    logger.info("Scheduler worker 已就绪")


async def main() -> None:
    stop_event = asyncio.Event()

    def _request_stop(*_args: object) -> None:
        stop_event.set()

    # SIGINT 在 Windows/Linux 都可用；SIGTERM 仅 POSIX，Windows 下注册不会报错但无效
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_stop)
        except (OSError, ValueError):
            pass

    try:
        await _run()
        await stop_event.wait()
    finally:
        logger.info("Scheduler worker 收到退出信号，正在关闭")
        scheduler_manager.shutdown()
        await close_redis()
        logger.info("Scheduler worker 已退出")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
