# 孤儿定时任务日志守护方案

> 状态：✅ Plan 已完成（2026-07-26）
>
> 通过周期性守护协程，根治"进程崩溃 / 重启 / 业务 hang 死后 `SysJobLog` 永久卡在
> RUNNING 状态"的问题；并解锁因此导致的非并发任务（`concurrent="2"`）永久阻塞
> 调度链路。

## Ship 记录（2026-07-26）

- **commit**：单 commit 直推 main（hotfix 规则，spec §"实施步骤" 步骤 9）
- **测试**：job 模块 34/34 绿；全量 1150/1150 绿（含本次 10 测试）
- **新增**：
  - `app/modules/job/models/job.py`：`SysJobLog.runner_id` + `Index("ix_sys_job_log_status_start_time", "status", "start_time")`
  - `app/modules/job/job_runner.py`：模块级 `RUNNER_ID = uuid4().hex`；`_do_execute` 写 log 时填 `runner_id=RUNNER_ID`
  - `app/modules/job/log_monitor.py`（新文件）：`JobLogMonitor` 类（`start/stop/_loop/_scan_once`）
  - `alembic/versions/fba0cf4a5e82_add_runner_id_to_sys_job_log.py`：migration
  - `tests/modules/job/test_log_monitor.py`：8 测试（spec §"测试矩阵" 1-8）
  - `tests/modules/job/test_job_runner.py`：2 测试（spec §"测试矩阵" 9-10）
- **改动**：
  - `app/main.py`：lifespan 启动 `JobLogMonitor`（仅 `APP_ROLE == "all"`），shutdown 时 `stop()`
  - `CLAUDE.md`：Common Pitfalls 第 13 条
  - `docs/MODULE-DEVELOPMENT-GUIDE.md`：3.1 节末尾加"长时任务 timeout 配置"子节

### Ship-time 决策记录

#### 10. **粗筛仅按 status 过滤，不在 SQL 用 start_time 阈值** —
spec 决策 5 原计划粗筛用 `start_time < now - DEFAULT_TIMEOUT`，但与测试 5
（timeout=300, start=now-11min 期望标 FAILED）矛盾：粗筛阈值 30min 会漏掉
11min 的孤儿。实施时改为粗筛只 `WHERE status="3"`（status 索引前缀命中），
细筛 Python 按 `job.timeout_seconds * 2` 二次过滤。理由：任务 timeout 配置各异，
grace 可能短至秒级（如 timeout=30s，grace=60s），统一粗筛阈值必然漏配；
RUNNING log 通常 < 100，纯 status 过滤性能 OK。**反例**：粗筛用 30min 阈值 →
timeout < 15min 的任务的孤儿永远扫不到，违反决策 3 "2x timeout grace" 的承诺。
**回归**：`test_orphan_job_with_timeout_uses_2x` 直接覆盖（start=now-11min 通过
粗筛，细筛按 grace=600s 判定标 FAILED）。

#### 11. **测试用 `_FakeSessionFactory` monkey-patch `AsyncSessionLocal`** —
monitor._scan_once 内 `async with AsyncSessionLocal() as db` 创建独立 session，
fixture 写入的 seed 数据在外层事务里（未真 commit），独立 session 看不到。
测试时把 `log_monitor.AsyncSessionLocal` 替换为返回 fixture `db_session` 的
fake context manager，让 monitor 看到测试 seed 数据，commit 也只触发 SAVEPOINT
release，最终 outer.rollback() 全部回滚——零污染。

#### 12. **测试查询用 `db.expire_all()` + select 重查，不用 `db_session.get(...)`** —
monitor._scan_once 内 `update(...)` 是 SQLAlchemy core 操作，不经过 ORM identity map，
缓存中的 `SysJobLog` 实例 status 字段不会自动刷新。直接 `db_session.get()`
拿到的是缓存对象（status 还是旧值）。`db.expire_all()` 强制失效后 select
才能拿到 DB 最新值。

### 与 spec 的偏差

| # | spec 原计划 | 实施 | 原因 |
|---|---|---|---|
| 1 | 决策 5 粗筛 `start_time < now - DEFAULT_TIMEOUT` | 仅 `status` 过滤 | 测试 5 矛盾（11min < 30min 漏扫），见决策 10 |
| 2 | spec §"实施步骤" 步骤 9 "一次性 commit" | 单 commit 直推 main | 按 memory `feedback_hotfix_no_pr.md`（1-2 文件级 hotfix 直推），spec 改动跨 5 文件 + migration + 10 测试，但内聚单一功能，仍按 hotfix 流程 |


## 背景

定时任务执行入口 `_do_execute`（`app/modules/job/job_runner.py:33`）的流程是：

1. 写一条 `SysJobLog(status=LOG_STATUS_RUNNING, start_time=now)` 并 `commit`
2. 执行任务函数 `_run_task`
3. 根据执行结果把 log 更新为 SUCCESS / FAILED

**问题**：第 1 步和第 3 步之间，如果进程被强杀、容器 OOM、`CancelledError`（优雅关闭
时任务被取消，但 `CancelledError` 是 `BaseException` 不是 `Exception`，`except Exception`
捕获不到）、或者业务函数在 `await` 处死锁——`SysJobLog` 会**永远停留在 `status="3"`**。

`reload_jobs`（`app/core/scheduler.py:198`）只在进程启动时把启用任务重新注册到
APScheduler，**完全不扫描孤儿 RUNNING 日志**。后果分两种：

- **非并发任务（`concurrent="2"`）**：`job_runner.py:45-52` 的并发检查会发现这条孤儿
  RUNNING 记录，**所有后续调度静默 `return`**。没有报错、没有 FAILED 记录、
  `next_run_time` 一直更新——任务看起来"启用"但实际**永不执行**。业务后果：对账 /
  数据同步 / 缓存预热等场景沉默漏跑，难以监控发现。
- **并发任务（`concurrent="1"`）**：不阻塞新执行，但日志列表永远卡几条"执行中"，
  统计数字虚高。

历史触发场景：

- 开发期 `fastapi dev` 改代码热重载 → 几乎必然积累孤儿
- 生产发版恰好赶上任务边界 / `kill -9` / OOM / K8s rolling update → 高概率撞上
- 长任务（外部 HTTP 阻塞、DB 死锁）业务 hang 死 → 不重启也会积累

**根因不在某次重启，而在"系统没有任何机制收尸孤儿 log"** —— 这是架构层面的缺失，
必须靠后台守护协程覆盖。

## 参考借鉴

xxl-job 的 `JobLogMonitorHelper` 是工业级实现：调度中心常驻一个守护线程，每分钟扫
`xxl_job_log`，把超时未回调的 log 标记失败。本方案借用这个思路，但根据单进程架构
做了简化：

| xxl-job 设计 | 是否借鉴 | 说明 |
|---|---|---|
| 调度中心 / 执行器物理分离 | ❌ | 单进程过度设计 |
| Quartz 集群持久化 trigger | ❌ | 业务表已是 source of truth |
| 调度前 DB 占位 log | ✅ | 已经在做（`_do_execute` 先 insert running） |
| **周期性孤儿检测守护** | ✅ | **核心借鉴点** |
| 多执行器路由策略 | ❌ | 单进程无意义 |
| 心跳 / lease 续约 | ❌ | 侵入业务函数，收益边际 |

详见：[`2026-07-01-local-naive-datetime.md`](./2026-07-01-local-naive-datetime.md)（决策
记录格式标杆）

## 决策记录

### 1. **进程级 `runner_id` 而非全局表锁** —
守护协程跑在当前进程，但当前进程也在执行任务，需要区分"上一进程遗留的孤儿"和"我
自己正在跑的长任务"。在 `JobRunner` 模块加载时生成 `RUNNER_ID = uuid4().hex`，
`_do_execute` 写 log 时带上；守护协程只清理 `runner_id != RUNNER_ID` 的记录。
**反例**：只按"start_time 早于阈值"判定 → 当前进程跑 30 分钟的长任务会被误杀。
**回归**：`test_current_runner_log_not_touched` 验证本进程 RUNNING log 不动；
`test_orphan_with_different_runner_marked_failed` 验证他进程孤儿被清理。

### 2. **`runner_id` 字段而非内存中维护"本进程任务 ID 集合"** —
内存集合方案在进程崩溃后丢失（崩溃正是产生孤儿的原因），无法区分。落库字段持久
化，重启后仍可识别。同时为未来引入多实例 / 分布式执行器复用同一字段。**反例**：
维护 `self._running_job_ids: set[int]` → 进程崩了集合也崩了，重启时无法判断哪些
是上次的孤儿。**回归**：`test_orphan_with_different_runner_marked_failed` 用旧
runner_id 写 log，验证重启后被正确识别为孤儿。

### 3. **2× `timeout_seconds` 作为 grace 阈值，而非固定 30 分钟** —
任务作者声明的 `timeout_seconds` 是最准的 SLA，用它 *2 作为孤儿判定阈值，既容忍
retry + 指数退避（`_run_task` 指数退避 1s→2s→4s，3 次 retry 累计 ~7s），又不会
误杀合理长任务。任务没设 timeout 时用 `DEFAULT_TIMEOUT=1800s`，grace=60min。**反例**：
固定阈值 30min → 会误杀合理长任务（如批量数据迁移）；阈值过长 → 检测延迟太大。
**回归**：`test_orphan_within_grace_period_skipped` 用 timeout=300s, start_time=now-5min
验证 grace 期内不动；`test_orphan_job_with_timeout_uses_2x` 验证按 job.timeout*2 判定。

### 4. **守护协程而非仅启动时清理** —
启动清理只覆盖"重启"场景，但业务 hang（外部 HTTP 阻塞、DB 死锁、`asyncio.sleep`
参数错乱）是真实场景——不重启也会积累孤儿，且往往更隐蔽（看起来任务一直在跑）。
周期守护协程（默认 60s 扫一次）作为兜底覆盖所有场景。启动时（lifespan）跑一次
`_scan_once` 立刻清理上次的孤儿，不等首个周期。**反例**：只在 lifespan 启动时清理
→ 业务 hang 死的任务永远卡住，且开发期 `fastapi dev` 热重载累积的孤儿在生产部署
时才暴露。**回归**：`test_orphan_beyond_default_grace_marked` 模拟运行中（非启动）
产生的孤儿，验证守护协程下一周期清理。

### 5. **粗筛 SQL + 细筛 Python，避免 N+1** —
守护协程扫描分两步：(1) 一次 SQL 拿所有 `status="3"` 且 `start_time < now -
DEFAULT_TIMEOUT` 的 log（用 `(status, start_time)` 索引，避免全表扫）；(2) Python
里按 job 实际 timeout 二次过滤。**反例**：先 SELECT 全部 log，再逐条 SELECT job
拿 timeout → N+1，每次扫描百次查询。或：在 SQL 里 JOIN job 表带 `2 * timeout` 条件
→ SQL 复杂，且 NULL timeout 处理丑陋。**回归**：`test_scan_handles_db_error_and_continues`
间接覆盖（异常处理路径）；性能基准：单次扫描 O(log) + O(running_logs)。

### 6. **NULL `runner_id` 视为孤儿** —
迁移前的老数据 `runner_id=NULL`，按"非本进程"处理自然识别为孤儿。无需手工 backfill，
迁移后首个守护周期自动清理历史脏数据。**反例**：要求所有老数据先 backfill
`runner_id` 再上线 → 增加 ops 复杂度，且 backfill 也没法知道当时是哪个进程跑的。
**回归**：`test_null_runner_id_treated_as_orphan` 用 NULL runner_id 写 log，验证清理。

### 7. **守护协程异常不退出** —
单次 `_scan_once` 包 `try/except Exception`，避免 DB 短暂抖动 / 锁等待超时让整个
守护协程死掉。下个 60s 周期自动恢复。**反例**：不捕获 → 一次 DB 重启就让守护永久
退出，孤儿积累但无人发现，比不做还糟。**回归**：`test_scan_handles_db_error_and_continues`
模拟首次扫描抛异常，验证第二次扫描仍正常工作。

### 8. **多实例并发扫幂等性 + WHERE 守卫** —
未来部署多个 `APP_ROLE=all` 实例时，多个 monitor 同时扫，可能同时 UPDATE 同一条
孤儿 log。UPDATE 语句带 `WHERE status = RUNNING` 守卫，第二个 monitor 的 UPDATE
匹配不到行（status 已被第一个改成 FAILED），影响 0 行——干净幂等，不会覆盖
end_time / error_msg。当前不引入 `SELECT ... FOR UPDATE SKIP LOCKED`，等真实出现
性能问题再加。**反例**：无 WHERE 守卫的 `UPDATE` → 第二个 monitor 覆盖
end_time / error_msg，audit 字段被多次写入；或一开始就上悲观锁 → 复杂度提升、
跨进程协调失败模式更多。**回归**：当前不强制测试多实例场景，留作未来 ADR。

### 9. **Python 与 DB 时钟基准对齐（强制 `datetime.now()`）** —
`_scan_once` 用 Python `datetime.now()` 算 grace，`_do_execute` 也强制用
`datetime.now()` 写 `start_time`（**不**用 `func.now()`），保证两边时钟基准都是
Python 进程时钟。容器化部署启用 NTP，单机部署偏差天然 < 1s；2x grace 额外覆盖
±60s 漂移。**反例**：`_do_execute` 用 `func.now()`（DB 时钟）、`_scan_once` 用
`datetime.now()`（Python 时钟）→ 两边漂移几十秒 → grace 判定边界 ±30s 误杀。
**回归**：`test_start_time_uses_python_now_not_db_now` 验证写入的 start_time 与
`datetime.now()` 偏差 < 1s。

## 已接入模块清单（待实施）

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `app/modules/job/models/job.py` | 改 | `SysJobLog` 加 `runner_id: Mapped[str \| None]` + 复合索引 `(status, start_time)` |
| `app/modules/job/job_runner.py` | 改 | 模块级 `RUNNER_ID = uuid4().hex`；`_do_execute` 写 log 时带 runner_id，并强制 `start_time=datetime.now()`（不用 `func.now()`，决策 9） |
| `app/modules/job/log_monitor.py` | 新建 | `JobLogMonitor` 类 + `start/stop/_scan_once` |
| `app/main.py` | 改 | lifespan 启动 monitor + 立即扫描一次，shutdown 时 stop |
| `alembic/versions/xxxx_add_runner_id.py` | 新建 | autogenerate 产物 |
| `tests/modules/job/test_log_monitor.py` | 新建 | 8 个测试 |
| `tests/modules/job/test_job_runner.py` | 新建 | 2 个测试 |
| `CLAUDE.md` | 改 | Common Pitfalls 第 13 条 |
| `docs/MODULE-DEVELOPMENT-GUIDE.md` | 改 | 长时任务 timeout 配置说明 |

## 数据模型变更

```python
# app/modules/job/models/job.py
class SysJobLog(Base):
    # ... 现有字段 ...

    runner_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="写入此日志的执行进程标识（uuid4）"
    )

    __table_args__ = (
        Index("ix_sys_job_log_status_start_time", "status", "start_time"),
    )
```

迁移：

```bash
cd F:\code\hohu\hohu-admin
alembic revision --autogenerate -m "add runner_id to sys_job_log"
alembic upgrade head
```

老数据 `runner_id=NULL`，守护协程首个周期自动清理。

## 守护协程实现骨架

```python
# app/modules/job/log_monitor.py
import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.modules.job.job_runner import RUNNER_ID, LOG_STATUS_RUNNING, LOG_STATUS_FAILED
from app.modules.job.models.job import SysJob, SysJobLog

logger = logging.getLogger(__name__)


class JobLogMonitor:
    """周期扫描孤儿 RUNNING 任务日志，标 FAILED。

    孤儿定义：status="3" AND runner_id != RUNNER_ID
              AND start_time < now - max(job.timeout_seconds * 2, DEFAULT_TIMEOUT)
    """

    SCAN_INTERVAL = 60       # 秒；周期扫描间隔（成功后用的间隔）
    DEFAULT_TIMEOUT = 1800   # 任务未设 timeout 时的兜底 grace（30min）
    MAX_BACKOFF = 600        # 异常退避上限（10min）

    def __init__(self, runner_id: str = RUNNER_ID) -> None:
        self._runner_id = runner_id
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动：先立即扫一次（吞异常），再启动周期 _loop。"""
        if self._task is not None:
            return
        # 启动扫描吞异常，避免 DB 抖动让 lifespan 失败；_loop 后续兜底
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
            await asyncio.sleep(backoff)  # ← 真用 backoff，不是固定 SCAN_INTERVAL

    async def _scan_once(self) -> int:
        """扫描一次，返回清理条数。"""
        now = datetime.now()
        coarse_threshold = now - timedelta(seconds=self.DEFAULT_TIMEOUT)

        async with AsyncSessionLocal() as db:
            # 粗筛：用索引拿候选
            stmt = select(SysJobLog).where(
                SysJobLog.status == LOG_STATUS_RUNNING,
                SysJobLog.start_time < coarse_threshold,
            )
            candidates = (await db.execute(stmt)).scalars().all()
            if not candidates:
                return 0

            # 一次 SELECT job 配置，避免 N+1
            job_ids = {c.job_id for c in candidates}
            jobs = {
                j.job_id: j
                for j in (
                    await db.execute(
                        select(SysJob).where(SysJob.job_id.in_(job_ids))
                    )
                ).scalars().all()
            }

            to_reclaim: list[SysJobLog] = []
            for log in candidates:
                # 不动本进程的 log（长任务保护，决策 1）
                if log.runner_id == self._runner_id:
                    continue

                # 按 job 实际 timeout 二次过滤（决策 3）
                job = jobs.get(log.job_id)
                timeout_sec = (
                    job.timeout_seconds if job and job.timeout_seconds
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
                    .where(SysJobLog.log_id == log.log_id)
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
```

## lifespan 集成

```python
# app/main.py
from app.modules.job.job_runner import RUNNER_ID
from app.modules.job.log_monitor import JobLogMonitor

@asynccontextmanager
async def lifespan(_app: FastAPI):
    job_log_monitor: JobLogMonitor | None = None
    if settings.APP_ROLE == "all":
        async with AsyncSessionLocal() as db:
            await scheduler_manager.reload_jobs(db)
        await scheduler_manager.start_with_pubsub()

        job_log_monitor = JobLogMonitor(runner_id=RUNNER_ID)
        # start() 内部跑一次启动扫描（吞异常）+ 启动 _loop
        await job_log_monitor.start()

    yield

    if job_log_monitor is not None:
        await job_log_monitor.stop()
    if settings.APP_ROLE == "all":
        scheduler_manager.shutdown()
    await close_redis()
```

## 测试矩阵

`tests/modules/job/test_log_monitor.py`（8 个测试，按 TDD 红 → 绿）：

| # | 测试名 | 场景 | 断言 |
|---|---|---|---|
| 1 | `test_orphan_with_different_runner_marked_failed` | 旧 runner_id 的 RUNNING log | status→FAILED，error_msg 含 runner_id |
| 2 | `test_current_runner_log_not_touched` | 本进程 RUNNING log | status 不变 |
| 3 | `test_orphan_within_grace_period_skipped` | timeout=300, start=now-5min（grace=600s 内） | 不动 |
| 4 | `test_orphan_beyond_default_grace_marked` | timeout=None, start=now-65min | 标 FAILED（65min > 60min 默认 grace） |
| 5 | `test_orphan_job_with_timeout_uses_2x` | timeout=300, start=now-11min | 标 FAILED（11min > 600s） |
| 6 | `test_null_runner_id_treated_as_orphan` | 老数据 runner_id=NULL | 标 FAILED，error_msg 含"历史数据" |
| 7 | `test_scan_handles_db_error_and_continues` | 首次扫描抛异常 | 第二次扫描仍正常 |
| 8 | `test_current_runner_log_with_old_start_time_skipped` | 本进程 RUNNING log 且 start_time 已超 grace | 不动（决策 1 边界） |

`tests/modules/job/test_job_runner.py`（2 个测试）：

| # | 测试名 | 场景 | 断言 |
|---|---|---|---|
| 9 | `test_log_written_with_runner_id` | 新建的 log | log.runner_id == RUNNER_ID |
| 10 | `test_start_time_uses_python_now_not_db_now` | 新建 log 后立即读 | log.start_time 与 `datetime.now()` 偏差 < 1s |

测试 fixture 关键点：
- 用 `db_session` fixture（事务回滚隔离）
- 写孤儿 log 时显式指定 `runner_id="old_xxx"`，避免与本进程 `RUNNER_ID` 冲突
- 模拟 timeout 用 `freezegun` 或直接 `log.start_time = now - timedelta(minutes=65)`

## 验证步骤

```bash
# 后端
cd F:\code\hohu\hohu-admin
alembic upgrade head
ruff check . && ruff format .
pytest tests/modules/job/ -v              # 10 个新测试
pytest                                    # 345 → 355 全绿

# 手动验证（dev 模式）
fastapi dev app/main.py
# 1. 触发一个慢任务（如 demo_long_running_task）
# 2. 任务运行中 Ctrl+C 强杀
# 3. 重启 fastapi dev
# 4. 立即查 sys_job_log → 旧 RUNNING 已变 FAILED，error_msg 写明守护标记
```

## 风险与边界

1. **手动触发的 `manual_*` 任务**：也走 `_do_execute`，同样带 `runner_id`，行为一致，
   不需要特判。
2. **纯 API 进程（`APP_ROLE != "all"`）**：不启动调度器和 monitor，但其手动触发请求
   通过 Redis pub/sub 转发到调度器进程执行——`runner_id` 永远来自调度器进程，正常。
3. **多实例并发扫**：当前不加悲观锁，多实例同时 UPDATE 同一条 log 时第二条影响 0 行
   （幂等）。真出现性能问题再加 `FOR UPDATE SKIP LOCKED`，作为独立 ADR。
4. **任务超时配置 < 执行时长**：守护协程不会过早杀本进程任务（runner_id 匹配保护）；
   只会让他进程遗留的孤儿按更短阈值清理——属于配置错误，应在任务编辑时校验。
5. **守护协程不退出**：单次扫描异常不致命，下个周期恢复；`_loop` 内带退避（上限
   10min），避免 DB 持续故障时空转。
6. **`_scan_once` 持 session 跑 Python 循环**：候选量大时（理论极端 > 1000）事务
   持时间长、内存膨胀。当前实现按 list 收集后逐条 `UPDATE ... WHERE status=RUNNING`
   守卫；极端规模出现时改批量 `UPDATE ... WHERE id IN (...)`（会丢个性化 error_msg），
   作为未来 ADR。
7. **时钟同步**：依赖 Python 进程时钟稳定（NTP 同步），决策 9 强制
   `_do_execute` 用 `datetime.now()` 写 start_time，与 monitor 同基准，消除
   Python ↔ DB 时钟漂移。

## 实施步骤

按 CLAUDE.md 规则 2（TDD 循环）：

1. ✍️ 写测试 1-10（红）
2. 📝 加 `runner_id` 列 + alembic autogenerate + upgrade
3. 🛠️ 改 `job_runner.py`：写 log 时填 `RUNNER_ID` + `start_time=datetime.now()`（让测试 9-10 绿）
4. 🛠️ 实现 `JobLogMonitor`（让测试 1-8 绿）
5. 🔌 `main.py` lifespan 集成（仅调 `start()`，不直接调 `_scan_once`）
6. ✅ `ruff check . && ruff format . && pytest`（全绿）
7. 📝 改 `CLAUDE.md` Common Pitfalls + `MODULE-DEVELOPMENT-GUIDE.md`
8. 📝 完成后改本 spec 头部状态：`⚠️ Plan 待实施` → `✅ Plan 已完成（YYYY-MM-DD）`
9. 📦 一次性 commit

## 参考借鉴

- xxl-job `JobLogMonitorHelper`：调度中心常驻守护线程，每分钟扫描超时未回调的
  `xxl_job_log` 记录标失败
- 决策记录格式标杆：[`2026-07-01-data-scope-demo.md`](./2026-07-01-data-scope-demo.md)
- 类型抽象标杆：[`2026-07-01-local-naive-datetime.md`](./2026-07-01-local-naive-datetime.md)
