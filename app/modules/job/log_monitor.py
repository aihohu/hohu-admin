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

from app.core.tenant import PlatformContext
from app.core.write_admission import admitted, is_closed, require_open
from app.db.authorization_http import get_authorization_runtime
from app.modules.job.job_runner import (
    RUNNER_ID,
)

logger = logging.getLogger(__name__)


class JobLogMonitor:
    """周期扫描孤儿 RUNNING 任务日志，标 FAILED。

    孤儿定义：status="3" AND runner_id != RUNNER_ID
              AND start_time < now - max(job.timeout_seconds * 2, DEFAULT_TIMEOUT)
    """

    SCAN_INTERVAL = 60  # 秒；周期扫描间隔（成功后用的间隔）
    DEFAULT_TIMEOUT = 1800  # 任务未设 timeout 时的兜底 grace（30min）
    MAX_BACKOFF = 600  # 异常退避上限（10min）

    def __init__(
        self, *, platform: PlatformContext, runner_id: str = RUNNER_ID
    ) -> None:
        if not isinstance(platform, PlatformContext):
            raise TypeError("platform context is required")
        self._platform = platform
        self._runner_id = runner_id
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动：先立即扫一次（吞异常），再启动周期 _loop。

        启动扫描吞异常，避免 DB 抖动让 lifespan 失败；后续 _loop 兜底。
        """
        require_open()
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
        while not is_closed():
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

    @admitted
    async def _scan_once(self) -> int:
        return await get_authorization_runtime().job.scan(
            platform=self._platform, runner_id=self._runner_id
        )
