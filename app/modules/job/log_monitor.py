"""孤儿任务日志守护协程（spec docs/specs/2026-07-02-orphan-job-log-monitor.md）。

进程崩溃 / 重启 / 业务 hang 死后 `SysJobLog` 永远停在 RUNNING，导致非并发任务
（concurrent="2"）静默漏跑——本守护协程周期扫描 status="3" 且
runner_id != RUNNER_ID 且超 grace 阈值的 log，标 FAILED 并写明守护标记。

9 个核心决策见 spec，关键点：
  - 不动本进程 log（runner_id 匹配）— 长任务保护
  - grace = job.timeout_seconds * 2（容忍 retry + 指数退避），任务未配 timeout 用
    DEFAULT_TIMEOUT=1800s，grace=60min
  - 单次 _scan_once 包 try/except — DB 抖动不让守护死掉，下周期恢复
  - UPDATE 带 WHERE status=RUNNING 守卫 — 多实例并发幂等
"""

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select, update

from app.db.session import AsyncSessionLocal
from app.modules.job.job_runner import (
    LOG_STATUS_FAILED,
    LOG_STATUS_RUNNING,
    RUNNER_ID,
)
from app.modules.job.models.job import SysJob, SysJobLog

logger = logging.getLogger(__name__)


class JobLogMonitor:
    """周期扫描孤儿 RUNNING 任务日志，标 FAILED。

    孤儿定义：status="3" AND runner_id != RUNNER_ID
              AND start_time < now - max(job.timeout_seconds * 2, DEFAULT_TIMEOUT)
    """

    SCAN_INTERVAL = 60  # 秒；周期扫描间隔（成功后用的间隔）
    DEFAULT_TIMEOUT = 1800  # 任务未设 timeout 时的兜底 grace（30min）
    MAX_BACKOFF = 600  # 异常退避上限（10min）

    def __init__(self, runner_id: str = RUNNER_ID) -> None:
        self._runner_id = runner_id
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动：先立即扫一次（吞异常），再启动周期 _loop。

        启动扫描吞异常，避免 DB 抖动让 lifespan 失败；后续 _loop 兜底。
        """
        if self._task is not None:
            return
        try:
            reclaimed = await self._scan_once()
            if reclaimed:
                logger.warning("JobLogMonitor 启动清理孤儿日志 %d 条", reclaimed)
        except Exception:
            logger.exception("JobLogMonitor 启动扫描失败，跳过，由 _loop 兜底")
        self._task = asyncio.create_task(self._loop())
        logger.info("JobLogMonitor 已启动 (runner_id=%s)", self._runner_id)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("JobLogMonitor 已停止")

    async def _loop(self) -> None:
        """周期扫描循环。单次异常触发退避（上限 MAX_BACKOFF）。"""
        backoff = self.SCAN_INTERVAL
        while True:
            try:
                reclaimed = await self._scan_once()
                if reclaimed:
                    logger.warning("JobLogMonitor 清理孤儿日志 %d 条", reclaimed)
                backoff = self.SCAN_INTERVAL  # 成功后重置
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("JobLogMonitor 扫描异常，%ss 后重试", backoff)
                backoff = min(backoff * 2, self.MAX_BACKOFF)
            await asyncio.sleep(backoff)

    async def _scan_once(self) -> int:
        """扫描一次，返回清理条数。

        粗筛：SQL 拿所有 status=RUNNING log（status 索引前缀命中）
        细筛：Python 按 job 实际 timeout * 2 二次过滤

        注：粗筛不在 SQL 用 start_time 过滤——任务 timeout 配置各异，grace 可能短
        至秒级（如 timeout=30s，grace=60s），统一粗筛阈值会漏。RUNNING log 通常
        很少（< 100），无性能问题。
        """
        now = datetime.now()

        async with AsyncSessionLocal() as db:
            # 粗筛：拿所有 RUNNING log（status 索引前缀命中）
            stmt = select(SysJobLog).where(SysJobLog.status == LOG_STATUS_RUNNING)
            candidates = (await db.execute(stmt)).scalars().all()
            if not candidates:
                return 0

            # 一次 SELECT job 配置，避免 N+1
            job_ids = {c.job_id for c in candidates}
            jobs = {
                j.job_id: j
                for j in (
                    await db.execute(select(SysJob).where(SysJob.job_id.in_(job_ids)))
                )
                .scalars()
                .all()
            }

            to_reclaim: list[SysJobLog] = []
            for log in candidates:
                # 不动本进程的 log（长任务保护，决策 1）
                if log.runner_id == self._runner_id:
                    continue

                # 按 job 实际 timeout 二次过滤（决策 3）
                job = jobs.get(log.job_id)
                timeout_sec = (
                    job.timeout_seconds
                    if job and job.timeout_seconds
                    else self.DEFAULT_TIMEOUT
                )
                grace = timeout_sec * 2
                if (now - log.start_time).total_seconds() < grace:
                    continue

                to_reclaim.append(log)

            for log in to_reclaim:
                runner_desc = (
                    "历史数据（迁移前遗留）"
                    if log.runner_id is None
                    else f"上一进程 runner_id={log.runner_id}"
                )
                # WHERE status=RUNNING 守卫（决策 8）：多实例并发时第二个 monitor
                # 影响 0 行，不覆盖 audit 字段
                await db.execute(
                    update(SysJobLog)
                    .where(SysJobLog.job_log_id == log.job_log_id)
                    .where(SysJobLog.status == LOG_STATUS_RUNNING)
                    .values(
                        status=LOG_STATUS_FAILED,
                        error_msg=(
                            f"执行进程未确认结果（{runner_desc}），"
                            f"疑似进程崩溃或重启，被守护协程标记失败"
                        ),
                        end_time=now,
                        duration=0,  # 实际执行时长未知；用 error_msg 文案澄清
                    )
                )

            if to_reclaim:
                await db.commit()
            return len(to_reclaim)
