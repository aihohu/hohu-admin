"""JobLogQuery / JobLogService 时间范围查询回归测试。

历史 bug：前端 NDatePicker 选 datetimerange 后用 new Date(ts).toISOString()
转成 ISO 8601 + 'Z' 后缀（tz-aware），Pydantic 解析成 aware datetime，
asyncpg 把它绑定到 sys_job_log.start_time（TIMESTAMP WITHOUT TIME ZONE）
时抛 TypeError: can't subtract offset-naive and offset-aware datetimes，
HTTP 层 500。

修复策略：Query schema 用统一的 LocalNaiveDatetime 类型（见
app/schemas/types.py），把 ms timestamp / ISO / datetime 统一转成 naive
本地 datetime。前端配套改为直接发 ms timestamp（不再 toISOString）。
"""

from datetime import datetime

import pytest

from app.core.id_generator import next_id
from app.modules.job.models.job import SysJob, SysJobLog
from app.modules.job.schemas.job import JobLogQuery
from app.modules.job.service.job_log_service import job_log_service
from tests.tenant_helpers import tenant_context

# seed_logs 是 setup fixture，测试不直接引用其返回值（靠 _seed_count 过滤）。
# 用 usefixtures 自动注入避免 ARG002 误报。
pytestmark = pytest.mark.usefixtures("seed_logs")


@pytest.fixture
async def seed_logs(db_session):
    """灌 3 条日志，时间分别在 7/1 早、7/1 中、7/2 早。

    用 unique job_key 隔离测试自清——查 result 时只看含此 key 的行，
    不被库内历史日志干扰（db_session 不隔离读）。
    """
    job_id = next_id()
    db_session.add(
        SysJob(
            tenant_id=0,
            job_id=job_id,
            job_name="t1",
            job_key=f"K_TEST_JOB_LOG_PARENT_{job_id}",
            trigger_type="cron",
            cron_expression="0 * * * *",
            status="1",
        )
    )
    db_session.add_all(
        [
            SysJobLog(
                tenant_id=0,
                job_id=job_id,
                job_name="t1",
                job_key="K_TEST_JOB_LOG_TIME_RANGE",
                status="1",
                start_time=datetime(2026, 7, 1, 0, 0, 0),
                end_time=datetime(2026, 7, 1, 0, 0, 1),
                duration=1000,
                attempt_count=1,
            ),
            SysJobLog(
                tenant_id=0,
                job_id=job_id,
                job_name="t1",
                job_key="K_TEST_JOB_LOG_TIME_RANGE",
                status="1",
                start_time=datetime(2026, 7, 1, 12, 0, 0),
                end_time=datetime(2026, 7, 1, 12, 0, 2),
                duration=2000,
                attempt_count=1,
            ),
            SysJobLog(
                tenant_id=0,
                job_id=job_id,
                job_name="t1",
                job_key="K_TEST_JOB_LOG_TIME_RANGE",
                status="1",
                start_time=datetime(2026, 7, 2, 0, 0, 0),
                end_time=datetime(2026, 7, 2, 0, 0, 1),
                duration=1000,
                attempt_count=1,
            ),
        ]
    )
    await db_session.flush()


SEED_KEY = "K_TEST_JOB_LOG_TIME_RANGE"


def _seed_count(records, *, key: str = SEED_KEY) -> int:
    return sum(1 for r in records if r.job_key == key)


class TestJobLogQueryAcceptsMultipleInputs:
    """LocalNaiveDatetime 类型层行为由 tests/schemas/test_types.py 覆盖；
    此处只验证 JobLogQuery 接入正确（字段类型生效）。"""

    def test_ms_timestamp_converted_to_local_naive(self):
        """前端真实输入：unix ms timestamp → naive 本地 datetime。"""
        # 2026-07-01 00:00:00 本地（假设服务器 UTC+8）= 2026-06-30 16:00 UTC
        # = unix ms 1751280000000 (UTC+8) / 1751251200000 (UTC)
        # 用本地时间反推 ms，避免硬编码时区
        local_dt = datetime(2026, 7, 1, 0, 0, 0)
        ts_ms = int(local_dt.timestamp() * 1000)
        q = JobLogQuery(current=1, size=10, start_time=ts_ms)
        assert q.start_time == local_dt
        assert q.start_time.tzinfo is None

    def test_naive_datetime_passes_through(self):
        """后端内部直接构造的 naive datetime 原样通过。"""
        q = JobLogQuery(
            current=1,
            size=10,
            start_time=datetime(2026, 7, 1, 0, 0, 0),
            end_time=datetime(2026, 7, 1, 23, 59, 59),
        )
        assert q.start_time == datetime(2026, 7, 1, 0, 0, 0)
        assert q.start_time.tzinfo is None

    def test_iso_with_z_does_not_raise(self):
        """兼容旧前端 / curl 手测：ISO 'Z' 字符串仍可解析（不再 500）。"""
        q = JobLogQuery(
            current=1,
            size=10,
            start_time="2026-07-01T00:00:00.000Z",
        )
        assert q.start_time.tzinfo is None


class TestJobLogServiceGetListWithTimeRange:
    async def test_time_range_filter_returns_subset(self, db_session):
        """7/1 当天范围应只返回前 2 条。"""
        q = JobLogQuery(
            current=1,
            size=100,
            start_time=datetime(2026, 7, 1, 0, 0, 0),
            end_time=datetime(2026, 7, 1, 23, 59, 59),
        )
        page = await job_log_service.get_list(
            db_session, q, tenant=tenant_context(tenant_id=0)
        )
        assert _seed_count(page.records) == 2

    async def test_ms_timestamp_does_not_500(self, db_session):
        """前端真实场景：传 ms timestamp，service 不抛 TypeError。

        回归 bug：asyncpg 'can't subtract offset-naive and offset-aware datetimes'。
        """
        local_start = datetime(2026, 7, 1, 0, 0, 0)
        local_end = datetime(2026, 7, 1, 23, 59, 59)
        q = JobLogQuery(
            current=1,
            size=100,
            start_time=int(local_start.timestamp() * 1000),
            end_time=int(local_end.timestamp() * 1000),
        )
        assert q.start_time == local_start
        assert q.end_time == local_end
        page = await job_log_service.get_list(
            db_session, q, tenant=tenant_context(tenant_id=0)
        )
        assert _seed_count(page.records) == 2

    async def test_no_time_filter_sees_seed(self, db_session):
        """无过滤时 seed 的 3 条应全部可见。

        用 seed 计数而非精确等于 page.total——db_session 不隔离读，库内
        历史日志会让总数 > 3（SAVEPOINT 只回滚写入，不阻止读到已 commit 的数据）。
        """
        q = JobLogQuery(current=1, size=100)
        page = await job_log_service.get_list(
            db_session, q, tenant=tenant_context(tenant_id=0)
        )
        assert _seed_count(page.records) == 3
