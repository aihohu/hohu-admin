import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from redis.asyncio import Redis
from sqlalchemy import select

from app.constants import STATUS_ENABLED
from app.core.redis import redis_client
from app.db.session import AsyncSessionLocal
from app.modules.job.job_runner import execute_job, run_job_manual
from app.modules.job.models.job import SysJob

logger = logging.getLogger(__name__)

# 调度器跨进程协调频道
CHANNEL_JOB_CHANGED = "scheduler:job_changed"
CHANNEL_MANUAL_TRIGGER = "scheduler:manual_trigger"


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
    """封装 APScheduler，管理定时任务的启动、停止和调度。

    支持两种工作模式：
    - 嵌入式（dev）：随 FastAPI lifespan 启停，单进程
    - 独立式（prod）：由 app.scheduler_worker 启停，通过 Redis pub/sub
      接收 web 进程发来的配置变更和手动触发事件
    """

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self._pubsub_task: asyncio.Task | None = None

    def start(self) -> None:
        """启动调度器。"""
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("APScheduler 已启动")

    async def start_with_pubsub(self) -> None:
        """启动调度器并订阅 Redis 控制频道（prod 独立进程模式使用）。"""
        self.start()
        if self._pubsub_task is None:
            self._pubsub_task = asyncio.create_task(self._listen_events())

    def shutdown(self, wait: bool = True) -> None:
        """关闭调度器。"""
        if self._pubsub_task is not None:
            self._pubsub_task.cancel()
            self._pubsub_task = None
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            logger.info("APScheduler 已关闭")

    async def _listen_events(self) -> None:
        """订阅 Redis 频道，处理任务变更和手动触发事件。

        频道：
        - scheduler:job_changed — 收到后从 DB 重新加载所有启用的任务
        - scheduler:manual_trigger — 收到后立即执行一次指定 job_id
        """
        backoff = 1.0
        while True:
            pubsub = None
            try:
                pubsub = redis_client.pubsub()
                await pubsub.subscribe(CHANNEL_JOB_CHANGED, CHANNEL_MANUAL_TRIGGER)
                logger.info("已订阅调度器控制频道")
                backoff = 1.0
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    channel = message.get("channel")
                    data = message.get("data", "")
                    try:
                        if channel == CHANNEL_JOB_CHANGED:
                            async with AsyncSessionLocal() as db:
                                await self.reload_jobs(db)
                        elif channel == CHANNEL_MANUAL_TRIGGER:
                            try:
                                job_id = int(data)
                            except (TypeError, ValueError):
                                logger.warning(
                                    "manual_trigger 收到非法 job_id: %s", data
                                )
                                continue
                            self.run_now(job_id)
                    except Exception:
                        logger.exception("处理调度器事件失败 channel=%s", channel)
            except asyncio.CancelledError:
                logger.info("调度器事件监听任务被取消")
                raise
            except Exception:
                logger.exception("调度器事件监听异常，%ss 后重连", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                # 关键：每轮重连都必须释放上一份 pubsub 的订阅连接，
                # 否则连接池（max_connections=20）会被耗尽
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:
                        logger.warning("关闭 pubsub 连接失败", exc_info=True)

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

    async def reload_jobs(self, db) -> None:
        """与 DB 当前状态对齐：移除已删除/停用的任务，新增或更新启用任务。

        用于两种场景：
        - 进程启动时的初次加载（此时 existing 为空，所有启用任务走"新增"路径）
        - 运行时收到 pub/sub 事件后的重新对齐

        优化：对 trigger 字符串未变的已注册任务直接跳过，避免 APScheduler
        在 replace_existing 时重置 next_run_time（这会让无关字段编辑也扰动调度）。
        """
        stmt = select(SysJob).where(SysJob.status == STATUS_ENABLED)
        result = await db.execute(stmt)
        enabled_jobs = result.scalars().all()
        enabled_ids = {j.job_id for j in enabled_jobs}

        # 快照当前调度器中的 job_* 任务，用于差异比对
        existing: dict[str, object] = {
            s.id: s for s in self._scheduler.get_jobs() if s.id.startswith("job_")
        }

        # 移除已不存在或已停用的任务
        for job_id_str in existing:
            scheduled_job_id = int(job_id_str[4:])
            if scheduled_job_id not in enabled_ids:
                self._scheduler.remove_job(job_id_str)
                logger.info("已移除失效调度任务: job_id=%s", scheduled_job_id)

        # 仅对新增或 trigger 变化的任务调用 add_job；其余保持原 next_run_time。
        # 单任务 build_trigger 失败（如 cron 表达式损坏）只跳过该任务，不影响其他。
        added = 0
        updated = 0
        skipped = 0
        for job in enabled_jobs:
            try:
                trigger = build_trigger(job)
            except Exception:
                skipped += 1
                logger.error(
                    "调度配置无效，跳过该任务: job_id=%s, job_key=%s",
                    job.job_id,
                    job.job_key,
                    exc_info=True,
                )
                continue
            job_id_str = f"job_{job.job_id}"
            current = existing.get(job_id_str)
            if current is not None and str(current.trigger) == str(trigger):
                continue
            self._scheduler.add_job(
                execute_job,
                trigger=trigger,
                args=[job.job_id],
                id=job_id_str,
                replace_existing=True,
            )
            if current is not None:
                updated += 1
                logger.info("已更新调度任务: %s", job.job_key)
            else:
                added += 1
                logger.info(
                    "已注册调度任务: %s (type=%s)", job.job_key, job.trigger_type
                )

        logger.info(
            "调度任务已对齐 DB：启用 %d 个（新增 %d，更新 %d，跳过无效 %d）",
            len(enabled_jobs),
            added,
            updated,
            skipped,
        )

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


async def notify_job_changed(redis: Redis | None = None) -> None:
    """通知调度器任务配置已变更，需重新对齐 DB。

    应在 API 层 commit 之后调用，避免调度器读到未提交的数据。
    """
    client = redis or redis_client
    await client.publish(CHANNEL_JOB_CHANGED, "")


async def notify_manual_trigger(job_id: int, redis: Redis | None = None) -> None:
    """通知调度器立即执行一次指定任务。"""
    client = redis or redis_client
    await client.publish(CHANNEL_MANUAL_TRIGGER, str(job_id))
