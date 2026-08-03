"""import_state 状态机单测（Task 0b）。

覆盖 spec §2.26 + v2.2 P1-2 + §2.27 CAS 幂等基础：
- validate_transition 拒绝非法转换
- _transition_batch_status 成功转换（CREATED → PREVIEW_DONE / PREVIEW_DONE → RUNNING）
- _transition_batch_status CAS 互斥（同一 session 第二次调用 rowcount=0）

batch_table fixture 用 raw DDL 临时建最小化测试表（batch_id + status 两列），
不依赖 ORM（Task 2 才落地）。outer-transaction fixture 保证测试结束 outer.rollback()
撤销 DDL，不污染其他测试。
"""

import pytest
from sqlalchemy import text

from app.core.exceptions import BusinessRuleException
from app.modules.system.user.constants import ImportBatchStatus as S
from app.modules.system.user.import_state import (
    _transition_batch_status,
    validate_transition,
)


@pytest.fixture
async def batch_table(db_session) -> str:
    """建临时 sys_user_import_batch 测试表，返回表名供测试引用。

    outer-transaction fixture 保证测试结束 outer.rollback() 撤销 DDL，不污染。
    """
    await db_session.execute(text("DROP TABLE IF EXISTS sys_user_import_batch"))
    await db_session.execute(
        text(
            "CREATE TABLE sys_user_import_batch ("
            "  batch_id VARCHAR(64) PRIMARY KEY,"
            "  status VARCHAR(32) NOT NULL"
            ")"
        )
    )
    await db_session.flush()
    yield "sys_user_import_batch"
    await db_session.execute(text("DROP TABLE IF EXISTS sys_user_import_batch"))


async def _get_status(db_session, batch_id: str) -> str:
    row = (
        await db_session.execute(
            text("SELECT status FROM sys_user_import_batch WHERE batch_id = :bid"),
            {"bid": batch_id},
        )
    ).fetchone()
    return row[0] if row else ""


class TestValidateTransition:
    def test_terminal_to_any_rejected(self):
        with pytest.raises(BusinessRuleException) as exc:
            validate_transition(S.SUCCESS, S.RUNNING)
        assert exc.value.error_code == "AI_IMPORT_ILLEGAL_TRANSITION"

    def test_skip_level_rejected(self):
        with pytest.raises(BusinessRuleException) as exc:
            validate_transition(S.CREATED, S.RUNNING)
        assert exc.value.error_code == "AI_IMPORT_ILLEGAL_TRANSITION"

    def test_terminal_to_terminal_rejected(self):
        with pytest.raises(BusinessRuleException) as exc:
            validate_transition(S.EXPIRED, S.CANCELLED)
        assert exc.value.error_code == "AI_IMPORT_ILLEGAL_TRANSITION"

    def test_created_to_preview_done_allowed(self):
        validate_transition(S.CREATED, S.PREVIEW_DONE)

    def test_preview_done_to_running_allowed(self):
        validate_transition(S.PREVIEW_DONE, S.RUNNING)

    def test_running_to_terminal_allowed(self):
        validate_transition(S.RUNNING, S.SUCCESS)
        validate_transition(S.RUNNING, S.PARTIAL_SUCCESS)
        validate_transition(S.RUNNING, S.FAILED)


class TestTransitionBatchStatus:
    async def test_state_created_to_preview_done(self, db_session, batch_table):
        assert batch_table == "sys_user_import_batch"  # fixture 副作用：表已建
        await db_session.execute(
            text(
                "INSERT INTO sys_user_import_batch (batch_id, status) "
                "VALUES ('b1', 'CREATED')"
            )
        )
        await db_session.flush()

        ok = await _transition_batch_status(db_session, "b1", S.CREATED, S.PREVIEW_DONE)

        assert ok is True
        assert await _get_status(db_session, "b1") == "PREVIEW_DONE"

    async def test_state_preview_done_to_running(self, db_session, batch_table):
        assert batch_table == "sys_user_import_batch"
        await db_session.execute(
            text(
                "INSERT INTO sys_user_import_batch (batch_id, status) "
                "VALUES ('b2', 'PREVIEW_DONE')"
            )
        )
        await db_session.flush()

        ok = await _transition_batch_status(db_session, "b2", S.PREVIEW_DONE, S.RUNNING)

        assert ok is True
        assert await _get_status(db_session, "b2") == "RUNNING"

    async def test_state_cas_prevents_race(self, db_session, batch_table):
        """模拟并发：Worker A 先转 PREVIEW_DONE → RUNNING，Worker B 后到 rowcount=0。"""
        assert batch_table == "sys_user_import_batch"
        await db_session.execute(
            text(
                "INSERT INTO sys_user_import_batch (batch_id, status) "
                "VALUES ('b3', 'PREVIEW_DONE')"
            )
        )
        await db_session.flush()

        ok_a = await _transition_batch_status(
            db_session, "b3", S.PREVIEW_DONE, S.RUNNING
        )
        ok_b = await _transition_batch_status(
            db_session, "b3", S.PREVIEW_DONE, S.RUNNING
        )

        assert ok_a is True
        assert ok_b is False
        assert await _get_status(db_session, "b3") == "RUNNING"

    async def test_state_illegal_transition_rejected_before_db_write(
        self, db_session, batch_table
    ):
        """非法转换抛异常，DB 不被改。"""
        assert batch_table == "sys_user_import_batch"
        await db_session.execute(
            text(
                "INSERT INTO sys_user_import_batch (batch_id, status) "
                "VALUES ('b4', 'SUCCESS')"
            )
        )
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc:
            await _transition_batch_status(db_session, "b4", S.SUCCESS, S.RUNNING)
        assert exc.value.error_code == "AI_IMPORT_ILLEGAL_TRANSITION"
        assert await _get_status(db_session, "b4") == "SUCCESS"

    async def test_missing_batch_returns_false(self, db_session, batch_table):
        """batch_id 不存在时 rowcount=0，返回 False（CAS 失败语义）。"""
        assert batch_table == "sys_user_import_batch"
        ok = await _transition_batch_status(
            db_session, "nonexistent", S.CREATED, S.PREVIEW_DONE
        )
        assert ok is False
