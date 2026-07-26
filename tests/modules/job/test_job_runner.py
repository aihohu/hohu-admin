"""JobRunner._do_execute 写日志时填 runner_id + 强制 Python now（spec 测试矩阵 9-10）。

测试目标（决策 9）：
  - runner_id 落库（决策 1/2）
  - start_time 用 Python datetime.now()，不用 func.now()，保证 Python ↔ DB
    时钟基准对齐——守护协程按 Python now 算 grace，与 log.start_time 同基准。

mock 策略：
  - 把 job_runner 模块内的 AsyncSessionLocal 替换为返回 db_session 的 fake
    context manager，让 _do_execute 跑在 fixture session 内（数据回滚隔离）
  - 临时注册一个立即成功的 fake task function（通过 _task_functions 直接写入，
    避免装饰器名字冲突）
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.job.job_runner import RUNNER_ID, _do_execute
from app.modules.job.models.job import SysJob, SysJobLog
from app.modules.job.task_registry import _task_functions


class _FakeSessionFactory:
    """让 job_runner._do_execute 内的 AsyncSessionLocal() 返回 db_session。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> "_FakeSessionFactory":
        return self

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        return None


async def _seed_enabled_job(
    db: AsyncSession,
    *,
    job_id: int,
    job_key: str,
) -> SysJob:
    """seed 一条 status=enabled / concurrent=允许 的 SysJob，让 _do_execute 走主路径。"""
    job = SysJob(
        job_id=job_id,
        job_name=f"test_runner_{job_key}",
        job_key=job_key,
        trigger_type="cron",
        cron_expression="0 * * * *",
        status="1",  # STATUS_ENABLED
        concurrent="1",  # 允许并发，跳过 RUNNING 检查
        timeout_seconds=None,
    )
    db.add(job)
    await db.flush()
    return job


@pytest.fixture
def fake_task_function():
    """注册一个立即成功的 fake task function。

    Yields:
        job_key（_do_execute 通过此 key 找到 fake task）
    """
    key = f"TEST_RUNNER_TASK_{datetime.now().strftime('%H%M%S%f')}"

    async def _noop(*_args, **_kwargs):
        return None

    _task_functions[key] = _noop
    yield key
    _task_functions.pop(key, None)


class TestDoExecuteWritesRunnerIdAndPythonNow:
    """spec §"测试矩阵" 9-10。"""

    async def test_log_written_with_runner_id(
        self, db_session, monkeypatch, fake_task_function
    ):
        """#9 新建的 log.runner_id == RUNNER_ID（决策 1/2）。"""
        monkeypatch.setattr(
            "app.modules.job.job_runner.AsyncSessionLocal",
            _FakeSessionFactory(db_session),
        )

        job_key = fake_task_function
        job_id = 90001
        await _seed_enabled_job(db_session, job_id=job_id, job_key=job_key)

        await _do_execute(job_id)

        stmt = (
            select(SysJobLog)
            .where(SysJobLog.job_id == job_id)
            .order_by(SysJobLog.job_log_id.desc())
            .limit(1)
        )
        log = (await db_session.execute(stmt)).scalar_one()
        assert log.runner_id == RUNNER_ID
        assert log.status == "1"  # SUCCESS（fake task 立即成功）

    async def test_start_time_uses_python_now_not_db_now(
        self, db_session, monkeypatch, fake_task_function
    ):
        """#10 log.start_time 与 datetime.now() 偏差 < 2s（决策 9 回归保护）。

        如果有人把 `start_time=datetime.now()` 改成 `func.now()`（DB 时钟），
        在 DB 与 Python 时钟漂移大的环境（容器 / 跨时区部署）就会出现 > 1s 偏差。
        """
        monkeypatch.setattr(
            "app.modules.job.job_runner.AsyncSessionLocal",
            _FakeSessionFactory(db_session),
        )

        job_key = fake_task_function
        job_id = 90002
        await _seed_enabled_job(db_session, job_id=job_id, job_key=job_key)

        before = datetime.now()
        await _do_execute(job_id)
        after = datetime.now()

        stmt = (
            select(SysJobLog)
            .where(SysJobLog.job_id == job_id)
            .order_by(SysJobLog.job_log_id.desc())
            .limit(1)
        )
        log = (await db_session.execute(stmt)).scalar_one()

        # log.start_time 应在 [before, after] 区间内（python now 写入）
        # 容差 2s 防 DB 持久化丢微秒 + 测试调度抖动
        assert before - timedelta(seconds=2) <= log.start_time
        assert log.start_time <= after + timedelta(seconds=2)
