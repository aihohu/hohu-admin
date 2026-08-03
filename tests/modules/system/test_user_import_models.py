"""ORM roundtrip smoke 测试（Task 2）。

验证 ORM ↔ DB 表映射正确：
- UserImportBatch INSERT/SELECT 含 ImportBatchStatus enum
- UserImportBatchLog FK ondelete=CASCADE 自动删 log
- UserExportTask INSERT/SELECT 含 ExportTaskStatus enum
- User.employee_no UNIQUE 约束（NULL 允许多个）

测试用 db_session outer-transaction fixture（不落库）。
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.security import get_password_hash
from app.modules.system.models.user import User
from app.modules.system.user.constants import (
    ExportTaskStatus,
    ImportBatchStatus,
)
from app.modules.system.user.models import (
    UserExportTask,
    UserImportBatch,
    UserImportBatchLog,
)


def _sample_batch_kwargs(**overrides):
    base = {
        "batch_id": "b1",
        "operator_id": 1,
        "filename": "users.xlsx",
        "file_sha256": "abc",
        "total_rows": 10,
        "preview_token": "tok-1",
        "on_conflict": "skip",
        "reason": "HR 同步",
        "status": ImportBatchStatus.CREATED,
    }
    base.update(overrides)
    return base


class TestUserImportBatchOrm:
    async def test_insert_and_select_roundtrip(self, db_session):
        batch = UserImportBatch(**_sample_batch_kwargs())
        db_session.add(batch)
        await db_session.flush()

        fetched = await db_session.get(UserImportBatch, "b1")
        assert fetched is not None
        assert fetched.status == ImportBatchStatus.CREATED
        assert fetched.reason == "HR 同步"
        assert fetched.preview_token == "tok-1"

    async def test_status_enum_roundtrip_all_values(self, db_session):
        """验证 PostgreSQL ENUM 写入读取每个值（防 typo / 防脏数据）。"""
        for i, status in enumerate(ImportBatchStatus):
            batch = UserImportBatch(
                **_sample_batch_kwargs(
                    batch_id=f"b-status-{i}",
                    preview_token=f"tok-status-{i}",
                    status=status,
                )
            )
            db_session.add(batch)
        await db_session.flush()

        for i, status in enumerate(ImportBatchStatus):
            fetched = await db_session.get(UserImportBatch, f"b-status-{i}")
            assert fetched.status == status, f"ENUM roundtrip 失败：{status}"

    async def test_preview_token_unique(self, db_session):
        batch1 = UserImportBatch(**_sample_batch_kwargs(batch_id="b-u1"))
        batch2 = UserImportBatch(
            **_sample_batch_kwargs(batch_id="b-u2", preview_token="tok-1")  # 同 token
        )
        db_session.add_all([batch1, batch2])
        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestUserImportBatchLogOrm:
    async def test_insert_log_with_batch(self, db_session):
        batch = UserImportBatch(**_sample_batch_kwargs())
        db_session.add(batch)
        await db_session.flush()

        log = UserImportBatchLog(
            log_id="log-1",
            batch_id="b1",
            operator_id=1,
            event="CREATED",
            from_status=None,
            to_status=ImportBatchStatus.CREATED,
            detail={"filename": "users.xlsx", "total_rows": 10},
        )
        db_session.add(log)
        await db_session.flush()

        fetched = await db_session.get(UserImportBatchLog, "log-1")
        assert fetched.event == "CREATED"
        assert fetched.to_status == ImportBatchStatus.CREATED
        assert fetched.detail["total_rows"] == 10

    async def test_cascade_delete_when_batch_deleted(self, db_session):
        """FK ondelete=CASCADE：删 batch 自动删 log。"""
        batch = UserImportBatch(**_sample_batch_kwargs())
        db_session.add(batch)
        await db_session.flush()

        log = UserImportBatchLog(
            log_id="log-cd",
            batch_id="b1",
            operator_id=1,
            event="CREATED",
            from_status=None,
            to_status=ImportBatchStatus.CREATED,
            detail={},
        )
        db_session.add(log)
        await db_session.flush()

        # 删 batch（需要 commit 才能触发 FK CASCADE，但 outer-transaction 模式 commit 不真落库）
        # 用 raw DELETE 验证
        await db_session.execute(
            text("DELETE FROM sys_user_import_batch WHERE batch_id='b1'")
        )
        await db_session.flush()

        # 清空 identity map（session 可能仍缓存被 CASCADE 删除的 log 实例）
        db_session.expunge_all()

        # log 应该被 CASCADE 删除
        fetched = await db_session.get(UserImportBatchLog, "log-cd")
        assert fetched is None


class TestUserExportTaskOrm:
    async def test_insert_and_select_roundtrip(self, db_session):
        task = UserExportTask(
            export_id="e1",
            operator_id=1,
            filter_snapshot={"dept_id": "1", "status": "1"},
            reason="导出通讯录",
            status=ExportTaskStatus.CREATED,
        )
        db_session.add(task)
        await db_session.flush()

        fetched = await db_session.get(UserExportTask, "e1")
        assert fetched.status == ExportTaskStatus.CREATED
        assert fetched.filter_snapshot["dept_id"] == "1"
        assert fetched.row_count is None

    async def test_status_enum_roundtrip_all_values(self, db_session):
        for i, status in enumerate(ExportTaskStatus):
            task = UserExportTask(
                export_id=f"e-status-{i}",
                operator_id=1,
                filter_snapshot={},
                reason="导出",
                status=status,
            )
            db_session.add(task)
        await db_session.flush()

        for i, status in enumerate(ExportTaskStatus):
            fetched = await db_session.get(UserExportTask, f"e-status-{i}")
            assert fetched.status == status


class TestUserEmployeeNo:
    """sys_user.employee_no UNIQUE 约束 + 多个 NULL 允许（spec §2.24）。"""

    async def test_employee_no_unique_constraint(self, db_session):
        """两个 user 同 employee_no 应触发 UNIQUE 违反。"""
        u1 = User(
            user_name="alice",
            employee_no="E001",
            hashed_password=get_password_hash("x"),
            status="1",
        )
        u2 = User(
            user_name="bob",
            employee_no="E001",  # 同 employee_no
            hashed_password=get_password_hash("x"),
            status="1",
        )
        db_session.add_all([u1, u2])
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_multiple_null_employee_no_allowed(self, db_session):
        """spec §2.24：UNIQUE 但允许多个 NULL（PostgreSQL 默认行为）。"""
        u1 = User(
            user_name="charlie",
            employee_no=None,
            hashed_password=get_password_hash("x"),
            status="1",
        )
        u2 = User(
            user_name="dave",
            employee_no=None,  # 同 NULL
            hashed_password=get_password_hash("x"),
            status="1",
        )
        db_session.add_all([u1, u2])
        await db_session.flush()  # 不应抛 IntegrityError

        result = await db_session.execute(
            select(User).where(User.employee_no.is_(None))
        )
        users = result.scalars().all()
        assert {u.user_name for u in users} >= {"charlie", "dave"}
