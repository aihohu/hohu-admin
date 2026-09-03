"""用户导入导出清理 cron 行为测试。

验证 3 个 cleanup 函数的 DB 行为 + 文件清理：
- ``cleanup_expired_batches``：90 天前终态 batch → 删 DB + failed_rows_file + preview 文件
- ``cleanup_expired_previews``：PREVIEW_DONE > 10min → EXPIRED + 删 preview 文件 + 写 batch_log
- ``cleanup_expired_export_tasks``：30 天前 ExportTask → 删 DB + export 文件

设计要点：
- service 层函数接 ``db: AsyncSession``（不 commit，由 task wrapper / API 层 commit）
- 测试用 ``db_session`` fixture 的 outer-transaction 模式（rollback 不落库）
- 文件清理用 ``MockFileStorage`` 注入（reset_file_storage_for_test 设置单例）
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete as _delete
from sqlalchemy import select

from app.core.file_storage import MockFileStorage, reset_file_storage_for_test
from app.core.tenant import PlatformContext
from app.modules.system.constants import (
    ExportTaskStatus,
    ImportBatchStatus,
)
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import (
    UserExportTask,
    UserImportBatch,
    UserImportBatchLog,
)
from app.modules.system.service.user_export_service import cleanup_expired_export_tasks
from app.modules.system.service.user_import_service import (
    cleanup_expired_batches,
    cleanup_expired_previews,
)

#: 终态批次保留 90 天。
BATCH_RETENTION_DAYS = 90
#: PREVIEW_DONE 批次 10 分钟后过期。
PREVIEW_TTL_MINUTES = 10
#: 导出文件保留 30 天。
EXPORT_RETENTION_DAYS = 30
TEST_OPERATOR_ID = 7_500_000_000_000_000_001
PLATFORM = PlatformContext(
    actor_principal_id=0,
    actor_name="test-retention",
    principal_type="service",
    permissions=frozenset({"platform:data:retention"}),
    reason="tenant-aware retention test",
    ticket_id="TEST-RETENTION",
    correlation_id="plan2-system-cleanup",
)


def _make_batch(
    *,
    batch_id: str,
    status: ImportBatchStatus = ImportBatchStatus.SUCCESS,
    preview_token: str | None = None,
    file_storage_key: str | None = None,
    failed_rows_file: str | None = None,
    created_at: datetime | None = None,
    operator_id: int = TEST_OPERATOR_ID,
) -> UserImportBatch:
    """构造 UserImportBatch 行（created_at 显式传，便于测试时间窗）。"""
    return UserImportBatch(
        tenant_id=0,
        batch_id=batch_id,
        operator_id=operator_id,
        filename=f"{batch_id}.xlsx",
        file_sha256=f"sha256-{batch_id}",
        records_hash=f"records-{batch_id}",
        total_rows=10,
        preview_token=preview_token or f"tok-{batch_id}",
        file_storage_key=file_storage_key,
        failed_rows_file=failed_rows_file,
        on_conflict="skip",
        reason="HR 月度同步",
        status=status,
        created_at=created_at or datetime.now(),
    )


def _make_export_task(
    *,
    export_id: str,
    file_storage_key: str | None = None,
    created_at: datetime | None = None,
    status: ExportTaskStatus = ExportTaskStatus.SUCCESS,
) -> UserExportTask:
    """构造 UserExportTask 测试行。"""
    return UserExportTask(
        tenant_id=0,
        export_id=export_id,
        operator_id=TEST_OPERATOR_ID,
        filter_snapshot={"user_name": None},
        reason="批量导出审计",
        file_storage_key=file_storage_key,
        row_count=100,
        status=status,
        created_at=created_at or datetime.now(),
    )


@pytest.fixture(autouse=True)
async def _cleanup_persisted_batches(db_session):
    """DELETE all persisted sys_user_import_batch / sys_user_export_task rows.

    Why: db_session is outer-transaction rollback (no test pollution), but the
    dev DB itself may already hold seeded batches from prior manual tests /
    E2E runs / Playwright smoke. cleanup_expired_previews / cleanup_expired_batches
    SELECT by status and created_at without test-scoped filtering, so any
    persisted PREVIEW_DONE/RUNNING rows leak into assertions (count == N
    instead of 0 or 1). DELETE first to keep tests deterministic.
    """
    await db_session.execute(_delete(UserImportBatch))
    await db_session.execute(_delete(UserExportTask))
    db_session.add(
        User(
            user_id=TEST_OPERATOR_ID,
            tenant_id=0,
            user_name="plan2_cleanup_operator",
            nickname="Plan 2 cleanup operator",
            hashed_password="not-used-by-tests",
            status="1",
        )
    )
    await db_session.flush()


@pytest.fixture
def mock_fs():
    """每个测试独立 MockFileStorage（注入到 file_storage 单例）。"""
    fs = MockFileStorage()
    reset_file_storage_for_test(fs)
    yield fs
    reset_file_storage_for_test(None)


# ========== cleanup_expired_batches ==========


class TestCleanupExpiredBatches:
    """删除 90 天前的终态批次。"""

    async def test_deletes_old_terminal_batch_with_files(self, db_session, mock_fs):
        """90 天前 SUCCESS batch → 删 DB 行 + failed_rows_file + preview 文件。"""
        old = datetime.now() - timedelta(days=BATCH_RETENTION_DAYS + 5)
        # 预放文件到 MockFileStorage
        await mock_fs.save(b"failed-rows", mime_type="...", namespace="import-error")
        # 拿真实 storage_key
        failed_key = await mock_fs.save(
            b"failed-rows", mime_type="...", namespace="import-error"
        )
        preview_key = await mock_fs.save(
            b"preview", mime_type="...", namespace="import-preview"
        )
        batch = _make_batch(
            batch_id="b-old-1",
            status=ImportBatchStatus.SUCCESS,
            failed_rows_file=failed_key,
            file_storage_key=preview_key,
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()

        count = await cleanup_expired_batches(db_session, platform=PLATFORM)

        assert count == 1
        # DB 行已删（CASCADE 自动删 batch_log，这里没 log 直接查 batch）
        assert await db_session.get(UserImportBatch, "b-old-1") is None
        # 文件也被删
        assert not await mock_fs.exists(failed_key)
        assert not await mock_fs.exists(preview_key)

    async def test_preserves_recent_batch_under_90_days(self, db_session):
        """89 天前的批次不删除。"""
        recent = datetime.now() - timedelta(days=BATCH_RETENTION_DAYS - 1)
        batch = _make_batch(
            batch_id="b-recent-1",
            status=ImportBatchStatus.SUCCESS,
            created_at=recent,
        )
        db_session.add(batch)
        await db_session.flush()

        count = await cleanup_expired_batches(db_session, platform=PLATFORM)

        assert count == 0
        assert await db_session.get(UserImportBatch, "b-recent-1") is not None

    async def test_skips_non_terminal_running_batch(self, db_session):
        """90 天前 RUNNING batch 不删（防活跃批次 / zombie 由 orphan 监控处理）。"""
        old = datetime.now() - timedelta(days=BATCH_RETENTION_DAYS + 5)
        batch = _make_batch(
            batch_id="b-running-old",
            status=ImportBatchStatus.RUNNING,
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()

        count = await cleanup_expired_batches(db_session, platform=PLATFORM)

        assert count == 0
        assert await db_session.get(UserImportBatch, "b-running-old") is not None

    async def test_skips_preview_done_batch(self, db_session):
        """90 天前 PREVIEW_DONE batch 不删（应由 preview cron 处理 → EXPIRED 后再被本 cron 删）。"""
        old = datetime.now() - timedelta(days=BATCH_RETENTION_DAYS + 5)
        batch = _make_batch(
            batch_id="b-preview-old",
            status=ImportBatchStatus.PREVIEW_DONE,
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()

        count = await cleanup_expired_batches(db_session, platform=PLATFORM)

        assert count == 0
        assert await db_session.get(UserImportBatch, "b-preview-old") is not None

    @pytest.mark.parametrize(
        "status",
        [
            ImportBatchStatus.SUCCESS,
            ImportBatchStatus.PARTIAL_SUCCESS,
            ImportBatchStatus.FAILED,
            ImportBatchStatus.EXPIRED,
            ImportBatchStatus.CANCELLED,
        ],
    )
    async def test_all_terminal_statuses_cleared(self, db_session, status):
        """5 个终态全部被清（参数化覆盖 TERMINAL_STATUSES）。"""
        old = datetime.now() - timedelta(days=BATCH_RETENTION_DAYS + 1)
        batch = _make_batch(
            batch_id=f"b-term-{status.value}",
            status=status,
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()

        count = await cleanup_expired_batches(db_session, platform=PLATFORM)

        assert count == 1
        assert await db_session.get(UserImportBatch, f"b-term-{status.value}") is None

    async def test_preserves_batch_without_files(self, db_session):
        """没文件附件的 batch 也正常删（file_storage_key / failed_rows_file 都 None）。"""
        old = datetime.now() - timedelta(days=BATCH_RETENTION_DAYS + 1)
        batch = _make_batch(
            batch_id="b-no-files",
            status=ImportBatchStatus.FAILED,
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()

        count = await cleanup_expired_batches(db_session, platform=PLATFORM)

        assert count == 1
        assert await db_session.get(UserImportBatch, "b-no-files") is None

    async def test_missing_file_does_not_break_cleanup(self, db_session):
        """failed_rows_file 指向不存在的文件（被外部删了）→ 不抛错，DB 行照删。"""
        old = datetime.now() - timedelta(days=BATCH_RETENTION_DAYS + 1)
        batch = _make_batch(
            batch_id="b-dangling-file",
            status=ImportBatchStatus.SUCCESS,
            failed_rows_file="import-error/already-deleted.xlsx",
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()

        # 不抛错（FileStorage.delete 返 False 而非 raise；MockFileStorage 同款）
        count = await cleanup_expired_batches(db_session, platform=PLATFORM)

        assert count == 1
        assert await db_session.get(UserImportBatch, "b-dangling-file") is None

    async def test_cascades_batch_log_on_delete(self, db_session):
        """删除批次后 batch_log 通过外键级联删除。"""
        old = datetime.now() - timedelta(days=BATCH_RETENTION_DAYS + 1)
        batch = _make_batch(
            batch_id="b-with-log",
            status=ImportBatchStatus.SUCCESS,
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()
        db_session.add(
            UserImportBatchLog(
                tenant_id=0,
                log_id="log-1",
                batch_id="b-with-log",
                operator_id=TEST_OPERATOR_ID,
                event="CREATED",
                detail={"k": "v"},
            )
        )
        await db_session.flush()

        await cleanup_expired_batches(db_session, platform=PLATFORM)

        # batch_log CASCADE 删除
        log_rows = (
            (
                await db_session.execute(
                    select(UserImportBatchLog).where(
                        UserImportBatchLog.batch_id == "b-with-log"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert log_rows == []


# ========== cleanup_expired_previews ==========


class TestCleanupExpiredPreviews:
    """PREVIEW_DONE 超过 10 分钟后迁移为 EXPIRED。"""

    async def test_marks_old_preview_done_as_expired(self, db_session):
        """PREVIEW_DONE + created_at < now-10min → EXPIRED + finished_at 写入。"""
        old = datetime.now() - timedelta(minutes=PREVIEW_TTL_MINUTES + 5)
        batch = _make_batch(
            batch_id="b-preview-expired",
            status=ImportBatchStatus.PREVIEW_DONE,
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()

        count = await cleanup_expired_previews(db_session, platform=PLATFORM)

        assert count == 1
        await db_session.refresh(batch)
        assert batch.status == ImportBatchStatus.EXPIRED
        assert batch.finished_at is not None

    async def test_preserves_fresh_preview_done(self, db_session):
        """PREVIEW_DONE 仅过去 5 分钟时保持不变。"""
        fresh = datetime.now() - timedelta(minutes=5)
        batch = _make_batch(
            batch_id="b-preview-fresh",
            status=ImportBatchStatus.PREVIEW_DONE,
            created_at=fresh,
        )
        db_session.add(batch)
        await db_session.flush()

        count = await cleanup_expired_previews(db_session, platform=PLATFORM)

        assert count == 0
        await db_session.refresh(batch)
        assert batch.status == ImportBatchStatus.PREVIEW_DONE

    async def test_deletes_orphan_preview_file(self, db_session, mock_fs):
        """批次过期时同时删除 preview 文件，避免垃圾文件残留。"""
        old = datetime.now() - timedelta(minutes=PREVIEW_TTL_MINUTES + 5)
        preview_key = await mock_fs.save(
            b"preview-data", mime_type="...", namespace="import-preview"
        )
        batch = _make_batch(
            batch_id="b-preview-file",
            status=ImportBatchStatus.PREVIEW_DONE,
            file_storage_key=preview_key,
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()

        await cleanup_expired_previews(db_session, platform=PLATFORM)

        assert not await mock_fs.exists(preview_key)

    async def test_writes_batch_log_expired_event(self, db_session):
        """写入 batch_log EXPIRED 事件进入审计链路。"""
        old = datetime.now() - timedelta(minutes=PREVIEW_TTL_MINUTES + 5)
        batch = _make_batch(
            batch_id="b-preview-log",
            status=ImportBatchStatus.PREVIEW_DONE,
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()

        await cleanup_expired_previews(db_session, platform=PLATFORM)

        logs = (
            (
                await db_session.execute(
                    select(UserImportBatchLog)
                    .where(UserImportBatchLog.batch_id == "b-preview-log")
                    .where(UserImportBatchLog.event == "EXPIRED")
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 1
        log = logs[0]
        assert log.from_status == ImportBatchStatus.PREVIEW_DONE
        assert log.to_status == ImportBatchStatus.EXPIRED
        assert "reason" in log.detail  # 审计字段

    async def test_skips_already_cancelled_batch(self, db_session, mock_fs):
        """PREVIEW_DONE → CANCELLED 已经发生（用户主动 cancel）→ CAS 失败 → 不写 log / 不删文件。"""
        old = datetime.now() - timedelta(minutes=PREVIEW_TTL_MINUTES + 5)
        preview_key = await mock_fs.save(
            b"preview-data", mime_type="...", namespace="import-preview"
        )
        batch = _make_batch(
            batch_id="b-already-cancelled",
            status=ImportBatchStatus.CANCELLED,  # 已经不是 PREVIEW_DONE
            file_storage_key=preview_key,
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()

        count = await cleanup_expired_previews(db_session, platform=PLATFORM)

        # CAS 不匹配，跳过；count=0；文件保留（cancel 流程自己负责）
        assert count == 0
        assert await mock_fs.exists(preview_key)

    async def test_skips_running_batch(self, db_session):
        """预检清理任务不处理 RUNNING 批次。"""
        old = datetime.now() - timedelta(minutes=PREVIEW_TTL_MINUTES + 5)
        batch = _make_batch(
            batch_id="b-running",
            status=ImportBatchStatus.RUNNING,
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()

        count = await cleanup_expired_previews(db_session, platform=PLATFORM)

        assert count == 0
        await db_session.refresh(batch)
        assert batch.status == ImportBatchStatus.RUNNING


# ========== cleanup_expired_export_tasks ==========


class TestCleanupExpiredExportTasks:
    """删除 30 天前的导出任务。"""

    async def test_deletes_old_export_task_with_file(self, db_session, mock_fs):
        """30 天前 ExportTask → 删 DB 行 + export 文件。"""
        old = datetime.now() - timedelta(days=EXPORT_RETENTION_DAYS + 5)
        export_key = await mock_fs.save(
            b"export-data", mime_type="...", namespace="export"
        )
        task = _make_export_task(
            export_id="exp-old-1",
            file_storage_key=export_key,
            created_at=old,
        )
        db_session.add(task)
        await db_session.flush()

        count = await cleanup_expired_export_tasks(db_session, platform=PLATFORM)

        assert count == 1
        assert await db_session.get(UserExportTask, "exp-old-1") is None
        assert not await mock_fs.exists(export_key)

    async def test_preserves_recent_export_task(self, db_session):
        """29 天前的导出任务不删除。"""
        recent = datetime.now() - timedelta(days=EXPORT_RETENTION_DAYS - 1)
        task = _make_export_task(
            export_id="exp-recent-1",
            created_at=recent,
        )
        db_session.add(task)
        await db_session.flush()

        count = await cleanup_expired_export_tasks(db_session, platform=PLATFORM)

        assert count == 0
        assert await db_session.get(UserExportTask, "exp-recent-1") is not None

    async def test_deletes_task_without_file(self, db_session):
        """没 file_storage_key（FAILED task 没生成文件）也能删。"""
        old = datetime.now() - timedelta(days=EXPORT_RETENTION_DAYS + 1)
        task = _make_export_task(
            export_id="exp-no-file",
            file_storage_key=None,
            status=ExportTaskStatus.FAILED,
            created_at=old,
        )
        db_session.add(task)
        await db_session.flush()

        count = await cleanup_expired_export_tasks(db_session, platform=PLATFORM)

        assert count == 1
        assert await db_session.get(UserExportTask, "exp-no-file") is None

    async def test_missing_file_does_not_break_cleanup(self, db_session):
        """file_storage_key 指向不存在的文件 → 不抛错，DB 行照删。"""
        old = datetime.now() - timedelta(days=EXPORT_RETENTION_DAYS + 1)
        task = _make_export_task(
            export_id="exp-dangling",
            file_storage_key="export/already-deleted.xlsx",
            created_at=old,
        )
        db_session.add(task)
        await db_session.flush()

        count = await cleanup_expired_export_tasks(db_session, platform=PLATFORM)

        assert count == 1
        assert await db_session.get(UserExportTask, "exp-dangling") is None
