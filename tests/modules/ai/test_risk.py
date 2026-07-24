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


# ============ v1.5+ SR-21: risk_appetite 三档修正（仅 high risk） ============


class TestClassifyExecutionModeRiskAppetite:
    """spec §5.3 SR-21: conservative/balanced/aggressive 仅影响 high risk"""

    # ----- balanced（默认，与 MVP 行为等价） -----

    def test_balanced_is_default(self) -> None:
        """不传 risk_appetite 等价 balanced（向后兼容）"""
        assert (
            classify_execution_mode(_meta(risk="high"), dry_run_count=1)
            == AiExecutionMode.AUTONOMOUS
        )

    def test_balanced_high_count_0_autonomous(self) -> None:
        assert (
            classify_execution_mode(
                _meta(risk="high"), dry_run_count=0, risk_appetite="balanced"
            )
            == AiExecutionMode.AUTONOMOUS
        )

    def test_balanced_high_count_2_hitl(self) -> None:
        assert (
            classify_execution_mode(
                _meta(risk="high"), dry_run_count=2, risk_appetite="balanced"
            )
            == AiExecutionMode.HITL
        )

    def test_balanced_high_count_none_hitl(self) -> None:
        assert (
            classify_execution_mode(
                _meta(risk="high"), dry_run_count=None, risk_appetite="balanced"
            )
            == AiExecutionMode.HITL
        )

    # ----- conservative（更严格：high 永远 HITL） -----

    def test_conservative_high_count_0_hitl(self) -> None:
        """conservative: 即使 dry_run_count=0 也走 HITL"""
        assert (
            classify_execution_mode(
                _meta(risk="high"), dry_run_count=0, risk_appetite="conservative"
            )
            == AiExecutionMode.HITL
        )

    def test_conservative_high_count_1_hitl(self) -> None:
        assert (
            classify_execution_mode(
                _meta(risk="high"), dry_run_count=1, risk_appetite="conservative"
            )
            == AiExecutionMode.HITL
        )

    def test_conservative_high_count_none_hitl(self) -> None:
        assert (
            classify_execution_mode(
                _meta(risk="high"), dry_run_count=None, risk_appetite="conservative"
            )
            == AiExecutionMode.HITL
        )

    # ----- aggressive（更宽松：high 永远 autonomous） -----

    def test_aggressive_high_count_2_autonomous(self) -> None:
        """aggressive: 即使 dry_run_count=2 也 autonomous"""
        assert (
            classify_execution_mode(
                _meta(risk="high"), dry_run_count=2, risk_appetite="aggressive"
            )
            == AiExecutionMode.AUTONOMOUS
        )

    def test_aggressive_high_count_none_autonomous(self) -> None:
        """aggressive: 即使 dry_run_count=None（未跑或失败）也 autonomous"""
        assert (
            classify_execution_mode(
                _meta(risk="high"), dry_run_count=None, risk_appetite="aggressive"
            )
            == AiExecutionMode.AUTONOMOUS
        )

    # ----- 不影响 low / destructive / hitl_always / injection_hit -----

    def test_appetite_does_not_affect_low(self) -> None:
        """risk=low 任何 appetite 都 autonomous"""
        for appetite in ("conservative", "balanced", "aggressive"):
            assert (
                classify_execution_mode(
                    _meta(risk="low"), dry_run_count=100, risk_appetite=appetite
                )
                == AiExecutionMode.AUTONOMOUS
            ), f"low risk with {appetite} should be autonomous"

    def test_appetite_does_not_affect_destructive(self) -> None:
        """destructive 任何 appetite 都 HITL（安全底线）"""
        for appetite in ("conservative", "balanced", "aggressive"):
            assert (
                classify_execution_mode(
                    _meta(risk="destructive"),
                    dry_run_count=0,
                    risk_appetite=appetite,
                )
                == AiExecutionMode.HITL
            ), f"destructive with {appetite} should be HITL"

    def test_appetite_does_not_override_hitl_always(self) -> None:
        """hitl_always=True 任何 appetite 都 HITL"""
        for appetite in ("conservative", "balanced", "aggressive"):
            assert (
                classify_execution_mode(
                    _meta(risk="low", hitl_always=True),
                    dry_run_count=0,
                    risk_appetite=appetite,
                )
                == AiExecutionMode.HITL
            ), f"hitl_always with {appetite} should be HITL"

    def test_appetite_does_not_override_injection_hit(self) -> None:
        """injection_hit=True 任何 appetite 都 HITL"""
        for appetite in ("conservative", "balanced", "aggressive"):
            assert (
                classify_execution_mode(
                    _meta(risk="low"),
                    dry_run_count=0,
                    injection_hit=True,
                    risk_appetite=appetite,
                )
                == AiExecutionMode.HITL
            ), f"injection_hit with {appetite} should be HITL"

    # ----- 防御性：未知值兜底为 balanced -----

    def test_unknown_appetite_falls_back_to_balanced(self) -> None:
        """未知 appetite 字符串兜底 balanced（不抛错）"""
        # high + count=1 + balanced → autonomous
        assert (
            classify_execution_mode(
                _meta(risk="high"), dry_run_count=1, risk_appetite="unknown_value"
            )
            == AiExecutionMode.AUTONOMOUS
        )
        # high + count=2 + balanced → HITL
        assert (
            classify_execution_mode(
                _meta(risk="high"), dry_run_count=2, risk_appetite="unknown_value"
            )
            == AiExecutionMode.HITL
        )
