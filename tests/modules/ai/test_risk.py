"""风险分级 helper 单元测试 — spec §5.3 矩阵"""

from app.modules.ai.agents.hitl.constants import AiExecutionMode
from app.modules.ai.agents.hitl.risk import classify_execution_mode
from app.modules.ai.agents.tools import AiToolMeta


def _meta(
    *,
    risk: str = "low",
    hitl_always: bool = False,
) -> AiToolMeta:
    return AiToolMeta(
        name="x.y",
        agent="a",
        summary="s",
        required_perms=("p",),
        risk=risk,  # type: ignore[arg-type]
        hitl_always=hitl_always,
    )


class TestClassifyExecutionMode:
    """spec §5.3 矩阵逐行覆盖"""

    def test_low_autonomous(self) -> None:
        """risk=low → autonomous"""
        assert classify_execution_mode(_meta(risk="low")) == AiExecutionMode.AUTONOMOUS

    def test_low_with_dry_run_count_autonomous(self) -> None:
        """risk=low 即使 dry_run_count>1 也 autonomous（low 不看 dry_run）"""
        assert (
            classify_execution_mode(_meta(risk="low"), dry_run_count=100)
            == AiExecutionMode.AUTONOMOUS
        )

    def test_high_count_le_1_autonomous(self) -> None:
        """risk=high + count≤1 → autonomous"""
        assert (
            classify_execution_mode(_meta(risk="high"), dry_run_count=1)
            == AiExecutionMode.AUTONOMOUS
        )

    def test_high_count_zero_autonomous(self) -> None:
        """risk=high + count=0 → autonomous"""
        assert (
            classify_execution_mode(_meta(risk="high"), dry_run_count=0)
            == AiExecutionMode.AUTONOMOUS
        )

    def test_high_count_gt_1_hitl(self) -> None:
        """risk=high + count>1 → HITL"""
        assert (
            classify_execution_mode(_meta(risk="high"), dry_run_count=2)
            == AiExecutionMode.HITL
        )

    def test_high_count_none_hitl(self) -> None:
        """risk=high + count=None（未跑 dry_run / 失败）→ 保守 HITL"""
        assert (
            classify_execution_mode(_meta(risk="high"), dry_run_count=None)
            == AiExecutionMode.HITL
        )

    def test_destructive_always_hitl(self) -> None:
        """risk=destructive → HITL（无视 dry_run_count）"""
        assert (
            classify_execution_mode(_meta(risk="destructive"), dry_run_count=0)
            == AiExecutionMode.HITL
        )
        assert (
            classify_execution_mode(_meta(risk="destructive"), dry_run_count=None)
            == AiExecutionMode.HITL
        )

    def test_hitl_always_overrides_low(self) -> None:
        """hitl_always=True 即使 risk=low 也强制 HITL"""
        assert (
            classify_execution_mode(_meta(risk="low", hitl_always=True))
            == AiExecutionMode.HITL
        )

    def test_hitl_always_overrides_high_count(self) -> None:
        """hitl_always=True 即使 risk=high+count=0 也强制 HITL"""
        assert (
            classify_execution_mode(
                _meta(risk="high", hitl_always=True), dry_run_count=0
            )
            == AiExecutionMode.HITL
        )

    def test_injection_hit_overrides_everything(self) -> None:
        """spec §11.1: injection_hit=True 永远 HITL（降级而非拒绝）"""
        assert (
            classify_execution_mode(_meta(risk="low"), injection_hit=True)
            == AiExecutionMode.HITL
        )

    def test_injection_hit_with_count_zero(self) -> None:
        """injection_hit=True 即使 risk=low+count=0 也 HITL"""
        assert (
            classify_execution_mode(
                _meta(risk="low"), dry_run_count=0, injection_hit=True
            )
            == AiExecutionMode.HITL
        )

    def test_priority_hitl_always_over_injection(self) -> None:
        """两个都命中时随便返一个 HITL（结果一致即可）"""
        result = classify_execution_mode(
            _meta(risk="low", hitl_always=True), injection_hit=True
        )
        assert result == AiExecutionMode.HITL
