"""operation_log_service 状态机迁移测试。

覆盖状态机的合法迁移、幂等和终态保护：
  autonomous:  running → success | failed
  HITL:        pending_confirmation → running → success | failed
                pending_confirmation → rejected | expired
"""

# ruff: noqa: ARG001, PLC0415

import pytest

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.hitl.constants import (
    AiExecutionMode,
    AiOperationStatus,
)
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.service.operation_log_service import (
    operation_log_service,
)


async def _start(
    db,
    *,
    status: AiOperationStatus = AiOperationStatus.PENDING_CONFIRMATION,
    confirmation_id: str | None = "conf_test_123",
    tool_call_id: str = "tc_test_001",
    user_id: int = 9001,
) -> int:
    """helper：写入一条 ai_operation_log"""
    return await operation_log_service.start_operation(
        db,
        trace_id="tr_test",
        conversation_id=100,
        user_id=user_id,
        tool_name="user.test",
        tool_call_id=tool_call_id,
        args_hash="a" * 64,
        args_summary="tool=user.test, risk=high, mode=hitl",
        risk_level="high",
        execution_mode=AiExecutionMode.HITL.value,
        status=status,
        confirmation_id=confirmation_id,
    )


class TestStartOperation:
    async def test_start_pending(self, db_session) -> None:
        log_id = await _start(db_session)
        assert log_id > 0

        log = await db_session.get(AiOperationLog, log_id)
        assert log is not None
        assert log.status == "pending_confirmation"
        assert log.confirmation_id == "conf_test_123"
        assert log.tool_call_id == "tc_test_001"

    async def test_start_running_autonomous(self, db_session) -> None:
        """autonomous 流初始 status=RUNNING, confirmation_id=None"""
        log_id = await operation_log_service.start_operation(
            db_session,
            trace_id="tr_a",
            conversation_id=1,
            user_id=1,
            tool_name="user.count",
            tool_call_id="tc_auto",
            args_hash="b" * 64,
            args_summary="tool=user.count, risk=low, mode=autonomous",
            risk_level="low",
            execution_mode=AiExecutionMode.AUTONOMOUS.value,
            status=AiOperationStatus.RUNNING,
            confirmation_id=None,
        )

        log = await db_session.get(AiOperationLog, log_id)
        assert log is not None
        assert log.status == "running"
        assert log.confirmation_id is None

    async def test_records_source_and_readonly_snapshot(self, db_session) -> None:
        log_id = await operation_log_service.start_operation(
            db_session,
            trace_id="tr_test_causality",
            conversation_id=123,
            source_user_message_id=456,
            readonly_snapshot=True,
            user_id=9001,
            tool_name="user.count",
            tool_call_id="tc_test_causality",
            args_hash="c" * 64,
            args_summary="tool=user.count, risk=low, mode=autonomous",
            risk_level="low",
            execution_mode=AiExecutionMode.AUTONOMOUS.value,
            status=AiOperationStatus.RUNNING,
        )

        log = await db_session.get(AiOperationLog, log_id)
        assert log.source_user_message_id == 456
        assert log.readonly_snapshot is True


class TestStateMachine:
    async def test_pending_to_running(self, db_session) -> None:
        log_id = await _start(db_session)

        await operation_log_service.mark_running(db_session, log_id)

        log = await db_session.get(AiOperationLog, log_id)
        assert log.status == "running"

    async def test_running_to_success(self, db_session) -> None:
        log_id = await _start(db_session)

        await operation_log_service.mark_running(db_session, log_id)
        await operation_log_service.mark_success(
            db_session, log_id, result_summary="ok", duration_ms=42
        )

        log = await db_session.get(AiOperationLog, log_id)
        assert log.status == "success"
        assert log.result_summary == "ok"
        assert log.duration_ms == 42
        assert log.finished_at is not None

    async def test_running_to_failed(self, db_session) -> None:
        log_id = await _start(db_session)

        await operation_log_service.mark_running(db_session, log_id)
        await operation_log_service.mark_failed(
            db_session,
            log_id,
            error_code="AI_INTERNAL_ERROR",
            duration_ms=10,
        )

        log = await db_session.get(AiOperationLog, log_id)
        assert log.status == "failed"
        assert log.error_code == "AI_INTERNAL_ERROR"

    async def test_pending_to_rejected(self, db_session) -> None:
        log_id = await _start(db_session)

        await operation_log_service.mark_rejected(db_session, log_id, approved_by=9001)

        log = await db_session.get(AiOperationLog, log_id)
        assert log.status == "rejected"
        assert log.approved_by == 9001
        assert log.finished_at is not None

    async def test_pending_to_expired(self, db_session) -> None:
        log_id = await _start(db_session)

        await operation_log_service.mark_expired(db_session, log_id)

        log = await db_session.get(AiOperationLog, log_id)
        assert log.status == "expired"

    async def test_mark_expired_if_pending_migrates_when_pending(
        self, db_session
    ) -> None:
        """修订 S-14 配套：pending_confirmation 状态下迁移到 expired"""
        log_id = await _start(db_session)

        result = await operation_log_service.mark_expired_if_pending(db_session, log_id)
        assert result is not None
        log = await db_session.get(AiOperationLog, log_id)
        assert log.status == "expired"

    async def test_mark_expired_if_pending_skips_when_running(self, db_session) -> None:
        """修订 S-14 配套：running 状态（已被 wake + mark_running）下不迁移

        场景：wake 失败（stream_gone）时 log 可能已被并发路径 mark_running，
        mark_expired_if_pending 应跳过（返回 None），不抛 TERMINAL_STATE。
        """
        log_id = await _start(db_session)
        await operation_log_service.mark_running(db_session, log_id)

        result = await operation_log_service.mark_expired_if_pending(db_session, log_id)
        assert result is None  # 没迁移
        log = await db_session.get(AiOperationLog, log_id)
        assert log.status == "running"  # 状态不变

    async def test_mark_rejected_if_pending_migrates_when_pending(
        self, db_session
    ) -> None:
        log_id = await _start(db_session)

        result = await operation_log_service.mark_rejected_if_pending(
            db_session,
            log_id,
            approved_by=9001,
        )

        assert result is not None
        log = await db_session.get(AiOperationLog, log_id)
        assert log.status == "rejected"
        assert log.approved_by == 9001

    async def test_mark_rejected_if_pending_skips_when_running(
        self, db_session
    ) -> None:
        log_id = await _start(db_session)
        await operation_log_service.mark_running(db_session, log_id)

        result = await operation_log_service.mark_rejected_if_pending(
            db_session,
            log_id,
            approved_by=9001,
        )

        assert result is None
        log = await db_session.get(AiOperationLog, log_id)
        assert log.status == "running"

    async def test_mark_expired_if_pending_skips_when_terminal(
        self, db_session
    ) -> None:
        """修订 S-14 配套：已终态（success / rejected 等）下不迁移"""
        log_id = await _start(db_session)
        await operation_log_service.mark_running(db_session, log_id)
        await operation_log_service.mark_success(
            db_session,
            log_id,
            result_summary="ok",
            duration_ms=100,
        )

        result = await operation_log_service.mark_expired_if_pending(db_session, log_id)
        assert result is None
        log = await db_session.get(AiOperationLog, log_id)
        assert log.status == "success"


class TestTransitionGuard:
    async def test_second_mark_running_is_rejected(self, db_session) -> None:
        log_id = await _start(db_session)
        await operation_log_service.mark_running(db_session, log_id)

        with pytest.raises(BusinessRuleException) as exc_info:
            await operation_log_service.mark_running(db_session, log_id)

        assert exc_info.value.error_code == "AI_OPERATION_LOG_ALREADY_RUNNING"

    async def test_terminal_cannot_transition(self, db_session) -> None:
        """终态后不能再迁移"""
        log_id = await _start(db_session)

        await operation_log_service.mark_rejected(db_session, log_id, approved_by=9001)

        with pytest.raises(BusinessRuleException) as exc_info:
            await operation_log_service.mark_running(db_session, log_id)
        assert exc_info.value.error_code == "AI_OPERATION_LOG_TERMINAL_STATE"


class TestAttachConfirmation:
    async def test_attach_confirmation(self, db_session) -> None:
        log_id = await _start(db_session, confirmation_id=None, tool_call_id="tc_att")

        await operation_log_service.attach_confirmation(db_session, log_id, "conf_xxx")

        log = await db_session.get(AiOperationLog, log_id)
        assert log.confirmation_id == "conf_xxx"

    async def test_attach_twice_raises(self, db_session) -> None:
        log_id = await _start(db_session, confirmation_id="conf_1")

        with pytest.raises(BusinessRuleException) as exc_info:
            await operation_log_service.attach_confirmation(
                db_session, log_id, "conf_2"
            )
        assert exc_info.value.error_code == "AI_OPERATION_LOG_CONFIRMATION_ALREADY_SET"


class TestMarkApproved:
    async def test_mark_approved_does_not_change_status(self, db_session) -> None:
        """approved_by 与 status.running 是两个事实，mark_approved 不改 status"""
        log_id = await _start(db_session)

        await operation_log_service.mark_approved(db_session, log_id, approved_by=9001)

        log = await db_session.get(AiOperationLog, log_id)
        assert log.approved_by == 9001
        # status 应保持原状（pending_confirmation）
        assert log.status == "pending_confirmation"


class TestGetByToolCallId:
    async def test_get_found(self, db_session) -> None:
        log_id = await _start(db_session, tool_call_id="tc_lookup")

        log = await operation_log_service.get_by_tool_call_id(db_session, "tc_lookup")
        assert log is not None
        assert log.log_id == log_id

    async def test_get_not_found(self, db_session) -> None:
        log = await operation_log_service.get_by_tool_call_id(
            db_session, "tc_nonexistent"
        )
        assert log is None

    async def test_get_with_owner_match(self, db_session) -> None:
        await _start(db_session, user_id=9001, tool_call_id="tc_owner")

        log = await operation_log_service.get_by_tool_call_id(
            db_session, "tc_owner", user_id=9001
        )
        assert log is not None

    async def test_get_with_owner_mismatch(self, db_session) -> None:
        await _start(db_session, user_id=9001, tool_call_id="tc_owner2")

        with pytest.raises(BusinessRuleException) as exc_info:
            await operation_log_service.get_by_tool_call_id(
                db_session, "tc_owner2", user_id=9999
            )
        assert exc_info.value.error_code == "AI_OPERATION_LOG_FORBIDDEN"


class TestGetNotFoundById:
    async def test_get_log_by_id_not_found(self, db_session) -> None:
        """不存在的 log_id 抛 AI_OPERATION_LOG_NOT_FOUND"""
        with pytest.raises(BusinessRuleException) as exc_info:
            await operation_log_service.mark_running(db_session, 99999999)
        assert exc_info.value.error_code == "AI_OPERATION_LOG_NOT_FOUND"
