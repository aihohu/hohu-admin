"""HITL 状态机 enum + DryRunResult 单元测试"""

import pytest

from app.modules.ai.agents.hitl.constants import (
    AiExecutionMode,
    AiOperationStatus,
    ConfirmAction,
    DryRunResult,
)


class TestAiOperationStatus:
    def test_terminal_states(self) -> None:
        """success/failed/rejected/expired 是终态"""
        assert AiOperationStatus.SUCCESS.is_terminal
        assert AiOperationStatus.FAILED.is_terminal
        assert AiOperationStatus.REJECTED.is_terminal
        assert AiOperationStatus.EXPIRED.is_terminal

    def test_non_terminal_states(self) -> None:
        """running/pending_confirmation 不是终态"""
        assert not AiOperationStatus.RUNNING.is_terminal
        assert not AiOperationStatus.PENDING_CONFIRMATION.is_terminal

    def test_str_enum_value(self) -> None:
        """StrEnum 的 .value 是字符串字面量（DB 列存这个值）"""
        assert AiOperationStatus.PENDING_CONFIRMATION.value == "pending_confirmation"
        assert AiExecutionMode.HITL.value == "hitl"
        assert ConfirmAction.APPROVED.value == "approved"

    def test_str_enum_str_comparison(self) -> None:
        """StrEnum 实例与字符串可直接比较（DB 列读出来是 str）"""
        assert AiOperationStatus.RUNNING == "running"
        assert AiExecutionMode.AUTONOMOUS == "autonomous"


class TestDryRunResult:
    def test_ok_with_count(self) -> None:
        dr = DryRunResult(ok=True, count=5)
        assert dr.ok
        assert dr.count == 5
        assert dr.reason is None

    def test_ok_false_with_reason(self) -> None:
        dr = DryRunResult(ok=False, reason="missing required field")
        assert not dr.ok
        assert dr.reason == "missing required field"

    def test_ok_false_without_reason_raises(self) -> None:
        """ok=False 时 reason 必填（用于 LLM 反问）"""
        with pytest.raises(ValueError, match="reason 必填"):
            DryRunResult(ok=False)

    def test_ok_false_with_count_raises(self) -> None:
        """ok=False 但 count > 0 自相矛盾"""
        with pytest.raises(ValueError, match="自相矛盾"):
            DryRunResult(ok=False, count=1, reason="x")

    def test_ok_true_with_reason(self) -> None:
        """ok=True 时 reason 可选（成功路径的辅助说明）"""
        dr = DryRunResult(ok=True, count=0, reason="no rows matched")
        assert dr.ok
        assert dr.count == 0
