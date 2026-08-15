"""OperationLogOut schema 字段类型测试。

started_at 在 pending_confirmation / expired / rejected 状态下可能为 NULL
（业务还没真正开始执行）。与 AiOperationLog model 的 nullable 一致。
"""

# ruff: noqa: ARG001, PLC0415

from datetime import datetime

from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.schemas.operation_log import OperationLogOut


def _make_log(
    *, started_at: datetime | None, status: str = "pending_confirmation"
) -> AiOperationLog:
    """构造一个最小可用 log 对象，from_attributes 转换需要字段齐"""
    return AiOperationLog(
        log_id=1,
        trace_id="tr_test",
        conversation_id=1,
        tenant_id=0,
        user_id=1,
        tool_name="user.batch_delete",
        tool_call_id="tc_test",
        args_hash="h" * 64,
        args_summary="tool=user.batch_delete",
        risk_level="destructive",
        execution_mode="hitl",
        status=status,
        started_at=started_at,
    )


class TestOperationLogOutStartedAtNullable:
    """started_at 可空：覆盖 pending_confirmation / expired / rejected 状态"""

    def test_started_at_none_pending_confirmation(self) -> None:
        """HITL 等待期间业务未启动，started_at=NULL 合法"""
        log = _make_log(started_at=None, status="pending_confirmation")
        out = OperationLogOut.model_validate(log)
        assert out.started_at is None
        assert out.status == "pending_confirmation"

    def test_started_at_none_expired(self) -> None:
        """HITL 5min 超时未操作 → expired，started_at 仍为 NULL"""
        log = _make_log(started_at=None, status="expired")
        out = OperationLogOut.model_validate(log)
        assert out.started_at is None
        assert out.status == "expired"

    def test_started_at_none_rejected(self) -> None:
        """用户拒绝 → rejected，started_at 仍为 NULL（业务未执行）"""
        log = _make_log(started_at=None, status="rejected")
        out = OperationLogOut.model_validate(log)
        assert out.started_at is None

    def test_started_at_set_running(self) -> None:
        """approved 后 started_at 设置，status=running"""
        now = datetime(2026, 7, 17, 12, 0, 0)
        log = _make_log(started_at=now, status="running")
        out = OperationLogOut.model_validate(log)
        assert out.started_at == now

    def test_started_at_set_success(self) -> None:
        """finished → started_at 必有值"""
        now = datetime(2026, 7, 17, 12, 0, 0)
        log = _make_log(started_at=now, status="success")
        out = OperationLogOut.model_validate(log)
        assert out.started_at == now
