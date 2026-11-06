"""Real fresh-session orphan scans over committed, exactly cleaned test rows."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import PlatformContext
from app.modules.job.job_runner import LOG_STATUS_FAILED, RUNNER_ID
from app.modules.job.log_monitor import JobLogMonitor as _JobLogMonitor
from app.modules.job.models.job import SysJob, SysJobLog
from app.modules.job.service.authorization_job_lifecycle import job_lifecycle_candidate
from tests.modules.job.authorization_fixture import (
    committed_job_session,  # noqa: F401
)

# unique job_key 前缀，避免 db_session 不隔离读时撞到历史日志
_TEST_KEY_PREFIX = "K_TEST_LOG_MONITOR"
_PLATFORM = PlatformContext(
    actor_principal_id=0,
    actor_name="test-log-monitor",
    principal_type="service",
    permissions=frozenset({"platform:job:operate"}),
    reason="job monitor test",
    ticket_id="TEST-JOB-MONITOR",
    correlation_id="test-job-monitor",
)


class JobLogMonitor(_JobLogMonitor):
    def __init__(self, runner_id: str = RUNNER_ID) -> None:
        super().__init__(platform=_PLATFORM, runner_id=runner_id)


def _gen_job_key(test_id: str) -> str:
    """每个测试用独立 job_key 隔离 seed 数据，避免相互污染。"""
    return f"{_TEST_KEY_PREFIX}_{test_id}"


async def _seed_log(
    db: AsyncSession,
    *,
    job_key: str,
    runner_id: str | None,
    start_time: datetime,
    job_id: int = 1,
    job_name: str = "test_job",
    timeout_seconds: int | None = None,
) -> tuple[SysJob, SysJobLog]:
    """seed 一条 SysJob + 一条 SysJobLog（status=RUNNING）。

    timeout_seconds 写到 SysJob（不是 SysJobLog），monitor 二次过滤时按
    job.timeout_seconds 计算 grace。
    """
    job = SysJob(
        tenant_id=0,
        job_id=job_id,
        job_name=job_name,
        job_key=job_key,
        trigger_type="cron",
        cron_expression="0 * * * *",
        status="1",
        concurrent="2",
        timeout_seconds=timeout_seconds,
    )
    log = SysJobLog(
        tenant_id=0,
        job_log_id=job_id * 1000,  # 测试用稳定 ID
        job_id=job_id,
        job_name=job_name,
        job_key=job_key,
        status="3",  # RUNNING
        start_time=start_time,
        attempt_count=1,
        runner_id=runner_id,
    )
    db.add(job)
    db.add(log)
    await db.flush()
    db.info["owned_jobs"].append((0, job.job_id, job.job_key))
    await db.commit()
    return job, log


async def _fetch_log(db: AsyncSession, job_log_id: int) -> SysJobLog:
    """重查 DB，绕过 ORM identity_map 缓存。

    monitor._scan_once 内 UPDATE 是 core 操作，缓存中的 SysJobLog 实例
    status 字段不会自动刷新。必须新 SELECT 才能拿到最新值。
    """
    # expire_all 让所有已缓存实例失效，下次访问时重新 SELECT
    db.expire_all()
    stmt = select(SysJobLog).where(
        SysJobLog.tenant_id == 0,
        SysJobLog.job_log_id == job_log_id,
    )
    return (await db.execute(stmt)).scalar_one()


class TestJobLogMonitorScanOnce:
    """测试 _scan_once 的核心行为。"""

    async def test_orphan_with_different_runner_marked_failed(self, db_session):
        """#1 旧 runner_id 的 RUNNING log → status=FAILED，error_msg 含 runner_id。"""

        job_key = _gen_job_key("orphan_diff_runner")
        old_start = datetime.now() - timedelta(minutes=70)
        await _seed_log(
            db_session,
            job_key=job_key,
            runner_id="OLD_RUNNER_AAAA",
            start_time=old_start,
        )

        monitor = JobLogMonitor(runner_id=RUNNER_ID)
        reclaimed = await monitor._scan_once()

        assert reclaimed == 1
        log = await _fetch_log(db_session, 1000)
        assert log.status == LOG_STATUS_FAILED
        assert log.end_time is not None
        assert "OLD_RUNNER_AAAA" in (log.error_msg or "")

    async def test_current_runner_log_not_touched(self, db_session):
        """#2 本进程 RUNNER_ID 的 RUNNING log → 不动（决策 1）。"""

        job_key = _gen_job_key("current_runner_safe")
        await _seed_log(
            db_session,
            job_key=job_key,
            runner_id=RUNNER_ID,  # 本进程
            start_time=datetime.now() - timedelta(minutes=70),
        )

        monitor = JobLogMonitor(runner_id=RUNNER_ID)
        reclaimed = await monitor._scan_once()

        assert reclaimed == 0
        log = await _fetch_log(db_session, 1000)
        assert log.status == "3"  # 仍 RUNNING

    async def test_orphan_within_grace_period_skipped(self, db_session):
        """#3 timeout=300, start=now-5min（grace=600s 内）→ 不动。"""

        job_key = _gen_job_key("orphan_within_grace")
        await _seed_log(
            db_session,
            job_key=job_key,
            runner_id="OLD_RUNNER_GRACE",
            start_time=datetime.now() - timedelta(minutes=5),
            timeout_seconds=300,  # grace = 300 * 2 = 600s = 10min
        )

        monitor = JobLogMonitor(runner_id=RUNNER_ID)
        reclaimed = await monitor._scan_once()

        assert reclaimed == 0
        log = await _fetch_log(db_session, 1000)
        assert log.status == "3"

    async def test_orphan_beyond_default_grace_marked(self, db_session):
        """#4 timeout=None, start=now-65min → 标 FAILED（65min > 60min 默认 grace）。"""

        job_key = _gen_job_key("orphan_beyond_default")
        await _seed_log(
            db_session,
            job_key=job_key,
            runner_id="OLD_RUNNER_DEFAULT",
            start_time=datetime.now() - timedelta(minutes=65),
            timeout_seconds=None,
        )

        monitor = JobLogMonitor(runner_id=RUNNER_ID)
        reclaimed = await monitor._scan_once()

        assert reclaimed == 1
        log = await _fetch_log(db_session, 1000)
        assert log.status == LOG_STATUS_FAILED

    async def test_orphan_job_with_timeout_uses_2x(self, db_session):
        """#5 timeout=300, start=now-11min → 标 FAILED（11min > grace=10min）。"""

        job_key = _gen_job_key("orphan_timeout_2x")
        await _seed_log(
            db_session,
            job_key=job_key,
            runner_id="OLD_RUNNER_2X",
            start_time=datetime.now() - timedelta(minutes=11),
            timeout_seconds=300,  # grace = 600s = 10min；11min 超过 → 标 FAILED
        )

        monitor = JobLogMonitor(runner_id=RUNNER_ID)
        reclaimed = await monitor._scan_once()

        assert reclaimed == 1
        log = await _fetch_log(db_session, 1000)
        assert log.status == LOG_STATUS_FAILED

    async def test_null_runner_id_treated_as_orphan(self, db_session):
        """#6 老数据 runner_id=NULL → 标 FAILED，error_msg 含"历史数据"。"""

        job_key = _gen_job_key("null_runner_id")
        await _seed_log(
            db_session,
            job_key=job_key,
            runner_id=None,  # 迁移前老数据
            start_time=datetime.now() - timedelta(minutes=70),
        )

        monitor = JobLogMonitor(runner_id=RUNNER_ID)
        reclaimed = await monitor._scan_once()

        assert reclaimed == 1
        log = await _fetch_log(db_session, 1000)
        assert log.status == LOG_STATUS_FAILED
        assert "历史数据" in (log.error_msg or "")

    async def test_scan_handles_db_error_and_continues(self, db_session, monkeypatch):
        """#7 首次扫描抛 DB 异常，第二次扫描仍正常工作（决策 7）。"""

        job_key = _gen_job_key("scan_recovers")
        await _seed_log(
            db_session,
            job_key=job_key,
            runner_id="OLD_RUNNER_REC",
            start_time=datetime.now() - timedelta(minutes=70),
        )

        monitor = JobLogMonitor(runner_id=RUNNER_ID)

        # 第一次：让 db.execute 抛 OperationalError，模拟 DB 抖动
        original_execute = job_lifecycle_candidate._orphans
        call_count = {"n": 0}

        async def _flaky_execute(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OperationalError("SELECT 1", {}, Exception("fake db error"))
            return await original_execute(*args, **kwargs)

        monkeypatch.setattr(job_lifecycle_candidate, "_orphans", _flaky_execute)

        with pytest.raises(OperationalError):
            await monitor._scan_once()

        # 恢复 db.execute
        monkeypatch.setattr(job_lifecycle_candidate, "_orphans", original_execute)

        # 第二次扫描应正常清理孤儿（决策 7：单次异常不退出）
        reclaimed = await monitor._scan_once()
        assert reclaimed == 1
        log = await _fetch_log(db_session, 1000)
        assert log.status == LOG_STATUS_FAILED

    async def test_current_runner_log_with_old_start_time_skipped(self, db_session):
        """#8 本进程 RUNNING log 且 start_time 已超 grace → 不动（决策 1 边界）。

        场景：本进程跑了一个超长任务（如 90 分钟数据迁移），守护协程不能误杀。
        runner_id 匹配保护是第一道防线，即使时间超过 grace 也不动。
        """

        job_key = _gen_job_key("current_runner_long_task")
        await _seed_log(
            db_session,
            job_key=job_key,
            runner_id=RUNNER_ID,
            start_time=datetime.now() - timedelta(hours=3),  # 远超 grace
            timeout_seconds=None,
        )

        monitor = JobLogMonitor(runner_id=RUNNER_ID)
        reclaimed = await monitor._scan_once()

        assert reclaimed == 0
        log = await _fetch_log(db_session, 1000)
        assert log.status == "3"  # 仍 RUNNING
