"""import_state 状态机测试。

覆盖合法迁移、终态保护和 CAS 幂等基础：
- validate_transition 拒绝非法转换
- _transition_batch_status 成功转换（CREATED → PREVIEW_DONE / PREVIEW_DONE → RUNNING）
- _transition_batch_status CAS 互斥（同一 session 第二次调用 rowcount=0）

batch_table fixture 用 raw DDL 临时建最小化测试表（batch_id + status 两列），
状态机本身不依赖 ORM；outer-transaction fixture 保证测试结束时回滚。
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
from app.modules.system.user.models import UserImportBatch


def _make_batch(batch_id: str, status: S, token: str | None = None) -> UserImportBatch:
    """构造一个完整 batch 实例（满足所有 NOT NULL 字段）。"""
    return UserImportBatch(
        batch_id=batch_id,
        operator_id=1,
        filename="test.xlsx",
        file_sha256="abc",
        records_hash="fake-records-hash",
        total_rows=10,
        preview_token=token or f"tok-{batch_id}",
        on_conflict="skip",
        reason="测试",
        status=status,
    )


@pytest.fixture
async def batch_table(db_session) -> str:
    """复用 sys_user_import_batch 表及其 batch_log 级联外键。

    fixture 仅清空表数据避免测试间残留；outer-transaction fixture 保证
    测试结束 outer.rollback() 撤销 INSERT，不真落库。
    """
    await db_session.execute(text("DELETE FROM sys_user_import_batch"))
    await db_session.flush()
    yield "sys_user_import_batch"
    # 不需要清空：outer.rollback() 会撤销本测试所有 INSERT


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

    def test_state_created_to_failed_on_parse_error(self):
        """CREATED → FAILED 是解析失败时的合法兜底路径。

        dry_run_import_users 内部若 ``_classify_records`` 抛异常（如 IO 错误 / 解析失败），
        理论上应主动把 batch 从 CREATED 转 FAILED 标记失败终态（当前代码未实现此分支，
        本测试只验证状态机允许该转换，集成路径由导入服务测试覆盖。

        **反例**: LEGAL_TRANSITIONS 不允许 CREATED → FAILED → 调用方只能删除 batch 行
        或留 CREATED 状态悬挂（cleanup cron 不删非终态，行永久驻留）。
        **回归**: ``constants.LEGAL_TRANSITIONS[CREATED]`` 含 FAILED。
        """
        validate_transition(S.CREATED, S.FAILED)  # should not raise


class TestTransitionBatchStatus:
    async def test_state_created_to_preview_done(self, db_session, batch_table):
        assert batch_table == "sys_user_import_batch"  # fixture 副作用：表已建
        db_session.add(_make_batch("b1", S.CREATED))
        await db_session.flush()

        ok = await _transition_batch_status(db_session, "b1", S.CREATED, S.PREVIEW_DONE)

        assert ok is True
        assert await _get_status(db_session, "b1") == "PREVIEW_DONE"

    async def test_state_preview_done_to_running(self, db_session, batch_table):
        assert batch_table == "sys_user_import_batch"
        db_session.add(_make_batch("b2", S.PREVIEW_DONE))
        await db_session.flush()

        ok = await _transition_batch_status(db_session, "b2", S.PREVIEW_DONE, S.RUNNING)

        assert ok is True
        assert await _get_status(db_session, "b2") == "RUNNING"

    async def test_state_cas_prevents_race(self, db_session, batch_table):
        """模拟并发：Worker A 先转 PREVIEW_DONE → RUNNING，Worker B 后到 rowcount=0。"""
        assert batch_table == "sys_user_import_batch"
        db_session.add(_make_batch("b3", S.PREVIEW_DONE))
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
        db_session.add(_make_batch("b4", S.SUCCESS))
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
