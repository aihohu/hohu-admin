"""用户导入导出清理 cron 测试（Task 22，spec §10 line 3112 + §2.22.1 + §2.26 + §2.31）。

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
from sqlalchemy import select

from app.core.file_storage import MockFileStorage, reset_file_storage_for_test
from app.modules.system.user.constants import (
    ExportTaskStatus,
    ImportBatchStatus,
)
from app.modules.system.user.export_service import cleanup_expired_export_tasks
from app.modules.system.user.import_service import (
    cleanup_expired_batches,
    cleanup_expired_previews,
)
from app.modules.system.user.models import (
    UserExportTask,
    UserImportBatch,
    UserImportBatchLog,
)

#: spec §2.22.1 line 1121：终态 batch 90 天后归档删除
BATCH_RETENTION_DAYS = 90
#: spec §2.26 line 1116：PREVIEW_DONE 10min 后过期
PREVIEW_TTL_MINUTES = 10
#: spec §2.31 line 1452 / 1554：导出文件 30 天 TTL
EXPORT_RETENTION_DAYS = 30


def _make_batch(
    *,
    batch_id: str,
    status: ImportBatchStatus = ImportBatchStatus.SUCCESS,
    preview_token: str | None = None,
    file_storage_key: str | None = None,
    failed_rows_file: str | None = None,
    created_at: datetime | None = None,
    operator_id: int = 1,
) -> UserImportBatch:
    """构造 UserImportBatch 行（created_at 显式传，便于测试时间窗）。"""
    return UserImportBatch(
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
    """构造 UserExportTask 行（spec §2.31）。"""
    return UserExportTask(
        export_id=export_id,
        operator_id=1,
        filter_snapshot={"user_name": None},
        reason="批量导出审计",
        file_storage_key=file_storage_key,
        row_count=100,
        status=status,
        created_at=created_at or datetime.now(),
    )


@pytest.fixture
def mock_fs():
    """每个测试独立 MockFileStorage（注入到 file_storage 单例）。"""
    fs = MockFileStorage()
    reset_file_storage_for_test(fs)
    yield fs
    reset_file_storage_for_test(None)


# ========== cleanup_expired_batches ==========


class TestCleanupExpiredBatches:
    """spec §2.22.1 line 1121 + line 781-797：90 天前终态 batch 删除。"""

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

        count = await cleanup_expired_batches(db_session)

        assert count == 1
        # DB 行已删（CASCADE 自动删 batch_log，这里没 log 直接查 batch）
        assert await db_session.get(UserImportBatch, "b-old-1") is None
        # 文件也被删
        assert not await mock_fs.exists(failed_key)
        assert not await mock_fs.exists(preview_key)

    async def test_preserves_recent_batch_under_90_days(self, db_session):
        """89 天前 batch 不删（边界条件，spec §2.22.1）。"""
        recent = datetime.now() - timedelta(days=BATCH_RETENTION_DAYS - 1)
        batch = _make_batch(
            batch_id="b-recent-1",
            status=ImportBatchStatus.SUCCESS,
            created_at=recent,
        )
        db_session.add(batch)
        await db_session.flush()

        count = await cleanup_expired_batches(db_session)

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

        count = await cleanup_expired_batches(db_session)

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

        count = await cleanup_expired_batches(db_session)

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

        count = await cleanup_expired_batches(db_session)

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

        count = await cleanup_expired_batches(db_session)

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
        count = await cleanup_expired_batches(db_session)

        assert count == 1
        assert await db_session.get(UserImportBatch, "b-dangling-file") is None

    async def test_cascades_batch_log_on_delete(self, db_session):
        """删 batch 行后 batch_log FK CASCADE 自动删（spec §2.28）。"""
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
                log_id="log-1",
                batch_id="b-with-log",
                operator_id=1,
                event="CREATED",
                detail={"k": "v"},
            )
        )
        await db_session.flush()

        await cleanup_expired_batches(db_session)

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
    """spec §2.26 line 1116 + v2.2 P1-2：PREVIEW_DONE 超 10min → EXPIRED。"""

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

        count = await cleanup_expired_previews(db_session)

        assert count == 1
        await db_session.refresh(batch)
        assert batch.status == ImportBatchStatus.EXPIRED
        assert batch.finished_at is not None

    async def test_preserves_fresh_preview_done(self, db_session):
        """PREVIEW_DONE + 5min 不动（边界，spec §2.26 + 10min TTL）。"""
        fresh = datetime.now() - timedelta(minutes=5)
        batch = _make_batch(
            batch_id="b-preview-fresh",
            status=ImportBatchStatus.PREVIEW_DONE,
            created_at=fresh,
        )
        db_session.add(batch)
        await db_session.flush()

        count = await cleanup_expired_previews(db_session)

        assert count == 0
        await db_session.refresh(batch)
        assert batch.status == ImportBatchStatus.PREVIEW_DONE

    async def test_deletes_orphan_preview_file(self, db_session, mock_fs):
        """EXPIRED 同时删 preview 文件（spec §2.22.1 反例 1：防文件垃圾残留）。"""
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

        await cleanup_expired_previews(db_session)

        assert not await mock_fs.exists(preview_key)

    async def test_writes_batch_log_expired_event(self, db_session):
        """写 batch_log EXPIRED event 进审计链路（spec §2.28）。"""
        old = datetime.now() - timedelta(minutes=PREVIEW_TTL_MINUTES + 5)
        batch = _make_batch(
            batch_id="b-preview-log",
            status=ImportBatchStatus.PREVIEW_DONE,
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()

        await cleanup_expired_previews(db_session)

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

        count = await cleanup_expired_previews(db_session)

        # CAS 不匹配，跳过；count=0；文件保留（cancel 流程自己负责）
        assert count == 0
        assert await mock_fs.exists(preview_key)

    async def test_skips_running_batch(self, db_session):
        """RUNNING 状态不扫（preview cron 只针对 PREVIEW_DONE，spec §2.26 v2.2 P1-2）。"""
        old = datetime.now() - timedelta(minutes=PREVIEW_TTL_MINUTES + 5)
        batch = _make_batch(
            batch_id="b-running",
            status=ImportBatchStatus.RUNNING,
            created_at=old,
        )
        db_session.add(batch)
        await db_session.flush()

        count = await cleanup_expired_previews(db_session)

        assert count == 0
        await db_session.refresh(batch)
        assert batch.status == ImportBatchStatus.RUNNING


# ========== cleanup_expired_export_tasks ==========


class TestCleanupExpiredExportTasks:
    """spec §2.31 line 1452 / 1554：30 天前 ExportTask 删除。"""

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

        count = await cleanup_expired_export_tasks(db_session)

        assert count == 1
        assert await db_session.get(UserExportTask, "exp-old-1") is None
        assert not await mock_fs.exists(export_key)

    async def test_preserves_recent_export_task(self, db_session):
        """29 天前 ExportTask 不删（边界，spec §2.31 30 天 TTL）。"""
        recent = datetime.now() - timedelta(days=EXPORT_RETENTION_DAYS - 1)
        task = _make_export_task(
            export_id="exp-recent-1",
            created_at=recent,
        )
        db_session.add(task)
        await db_session.flush()

        count = await cleanup_expired_export_tasks(db_session)

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

        count = await cleanup_expired_export_tasks(db_session)

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

        count = await cleanup_expired_export_tasks(db_session)

        assert count == 1
        assert await db_session.get(UserExportTask, "exp-dangling") is None
