"""JobLogMonitor 孤儿任务日志守护协程测试（spec 2026-07-02 §"测试矩阵" 1-8）。

测试目标：守护协程能识别"上一进程遗留的 RUNNING log"并标 FAILED，
同时不动本进程正在跑的长任务。

依赖：
  - db_session fixture（事务回滚隔离，monitor 用 monkey-patch 注入此 session）
  - SysJobLog + SysJob seed 数据（每测试自清，靠 unique job_key 隔离）

monkey-patch 策略：
  - `AsyncSessionLocal` 替换为返回 db_session 的 fake context manager
  - 这样 monitor._scan_once 看到测试 seed 数据，commit 也只触发 SAVEPOINT release

查询注意事项：
  - monitor._scan_once 内 UPDATE 是 SQLAlchemy core 操作，不会自动刷新 ORM
    identity_map 中的 SysJobLog 实例。所以查询必须用 select(...) 重查 DB，
    不能用 db_session.get(SysJobLog, ...)（拿到的是缓存对象，status 还是旧值）。
"""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.job.job_runner import LOG_STATUS_FAILED, RUNNER_ID
from app.modules.job.log_monitor import JobLogMonitor
from app.modules.job.models.job import SysJob, SysJobLog

# unique job_key 前缀，避免 db_session 不隔离读时撞到历史日志
_TEST_KEY_PREFIX = "K_TEST_LOG_MONITOR"


class _FakeSessionFactory:
    """让 monitor._scan_once 内的 `async with AsyncSessionLocal() as db`
    拿到测试 fixture 的 db_session。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> "_FakeSessionFactory":
        return self

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def _inject_session(monkeypatch, session: AsyncSession) -> None:
    """让 log_monitor 模块内的 AsyncSessionLocal 指向 db_session fixture。"""
    monkeypatch.setattr(
        "app.modules.job.log_monitor.AsyncSessionLocal",
        _FakeSessionFactory(session),
    )


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
    return job, log


async def _fetch_log(db: AsyncSession, job_log_id: int) -> SysJobLog:
    """重查 DB，绕过 ORM identity_map 缓存。

    monitor._scan_once 内 UPDATE 是 core 操作，缓存中的 SysJobLog 实例
    status 字段不会自动刷新。必须新 SELECT 才能拿到最新值。
    """
    # expire_all 让所有已缓存实例失效，下次访问时重新 SELECT
    db.expire_all()
    stmt = select(SysJobLog).where(SysJobLog.job_log_id == job_log_id)
    return (await db.execute(stmt)).scalar_one()


class TestJobLogMonitorScanOnce:
    """测试 _scan_once 的核心行为（spec §"测试矩阵" 1-8）。"""

    async def test_orphan_with_different_runner_marked_failed(
        self, db_session, monkeypatch
    ):
        """#1 旧 runner_id 的 RUNNING log → status=FAILED，error_msg 含 runner_id。"""
        _inject_session(monkeypatch, db_session)

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

    async def test_current_runner_log_not_touched(self, db_session, monkeypatch):
        """#2 本进程 RUNNER_ID 的 RUNNING log → 不动（决策 1）。"""
        _inject_session(monkeypatch, db_session)

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

    async def test_orphan_within_grace_period_skipped(self, db_session, monkeypatch):
        """#3 timeout=300, start=now-5min（grace=600s 内）→ 不动。"""
        _inject_session(monkeypatch, db_session)

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

    async def test_orphan_beyond_default_grace_marked(self, db_session, monkeypatch):
        """#4 timeout=None, start=now-65min → 标 FAILED（65min > 60min 默认 grace）。"""
        _inject_session(monkeypatch, db_session)

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

    async def test_orphan_job_with_timeout_uses_2x(self, db_session, monkeypatch):
        """#5 timeout=300, start=now-11min → 标 FAILED（11min > grace=10min）。"""
        _inject_session(monkeypatch, db_session)

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

    async def test_null_runner_id_treated_as_orphan(self, db_session, monkeypatch):
        """#6 老数据 runner_id=NULL → 标 FAILED，error_msg 含"历史数据"。"""
        _inject_session(monkeypatch, db_session)

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
        _inject_session(monkeypatch, db_session)

        job_key = _gen_job_key("scan_recovers")
        await _seed_log(
            db_session,
            job_key=job_key,
            runner_id="OLD_RUNNER_REC",
            start_time=datetime.now() - timedelta(minutes=70),
        )

        monitor = JobLogMonitor(runner_id=RUNNER_ID)

        # 第一次：让 db.execute 抛 OperationalError，模拟 DB 抖动
        original_execute = db_session.execute
        call_count = {"n": 0}

        async def _flaky_execute(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OperationalError("SELECT 1", {}, Exception("fake db error"))
            return await original_execute(*args, **kwargs)

        monkeypatch.setattr(db_session, "execute", _flaky_execute)

        with pytest.raises(OperationalError):
            await monitor._scan_once()

        # 恢复 db.execute
        monkeypatch.setattr(db_session, "execute", original_execute)

        # 第二次扫描应正常清理孤儿（决策 7：单次异常不退出）
        reclaimed = await monitor._scan_once()
        assert reclaimed == 1
        log = await _fetch_log(db_session, 1000)
        assert log.status == LOG_STATUS_FAILED

    async def test_current_runner_log_with_old_start_time_skipped(
        self, db_session, monkeypatch
    ):
        """#8 本进程 RUNNING log 且 start_time 已超 grace → 不动（决策 1 边界）。

        场景：本进程跑了一个超长任务（如 90 分钟数据迁移），守护协程不能误杀。
        runner_id 匹配保护是第一道防线，即使时间超过 grace 也不动。
        """
        _inject_session(monkeypatch, db_session)

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


# 用 _FakeSessionFactory 替换 AsyncSessionLocal 后，
# monitor._scan_once 内的 `async with AsyncSessionLocal() as db:`
# 不会真正创建新 session——而是返回 db_session。
# 这是必要的，否则 monitor 用独立 session 看不到 fixture 写入的数据
# （fixture 写入在外层事务里，未真正 commit）。


def test_fake_session_factory_yields_session():
    """单测 _FakeSessionFactory：作为 AsyncSessionLocal 替身时行为正确。

    验证：可被调用 + 支持 async with 上下文 + 返回注入的 session。
    """

    async def _run():
        sentinel = object()  # 用 object() 模拟 session，避免类型耦合
        factory = _FakeSessionFactory(sentinel)
        async with factory() as session:
            return session is sentinel

    assert asyncio.run(_run())
