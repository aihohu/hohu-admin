import pytest

from app.core.tenant_rollout_metrics import (
    BUILD_INFO,
    HOSTED_GATE_DECISIONS_TOTAL,
    HOSTED_ROLLOUT_INFO,
    configure_hosted_rollout_info,
    record_hosted_gate_decision,
)


def test_hosted_gate_metric_uses_only_frozen_low_cardinality_labels() -> None:
    assert set(HOSTED_GATE_DECISIONS_TOTAL._labelnames) == {  # type: ignore[attr-defined]
        "surface",
        "result",
    }
    assert set(HOSTED_ROLLOUT_INFO._labelnames) == {  # type: ignore[attr-defined]
        "mode",
        "login_gate",
        "target",
    }


def test_hosted_gate_metric_records_a_validated_decision() -> None:
    labels = ("access", "allowed")
    before = (
        HOSTED_GATE_DECISIONS_TOTAL.labels(*labels)._value.get()  # type: ignore[attr-defined]
        if labels in HOSTED_GATE_DECISIONS_TOTAL._metrics  # type: ignore[attr-defined]
        else 0
    )

    record_hosted_gate_decision(surface="access", result="allowed")

    after = HOSTED_GATE_DECISIONS_TOTAL.labels(*labels)._value.get()  # type: ignore[attr-defined]
    assert after == before + 1


@pytest.mark.parametrize(
    ("surface", "result"),
    [("tenant-22", "allowed"), ("access", "tenant-22")],
)
def test_hosted_gate_metric_rejects_caller_controlled_labels(
    surface: str, result: str
) -> None:
    with pytest.raises(ValueError, match="invalid hosted rollout metric label"):
        record_hosted_gate_decision(surface=surface, result=result)  # type: ignore[arg-type]


def test_rollout_info_exposes_configuration_state_without_tenant_identity() -> None:
    configure_hosted_rollout_info(
        mode="hosted",
        login_gate=True,
        target_configured=True,
        build_sha="a" * 40,
    )

    assert (
        HOSTED_ROLLOUT_INFO.labels(
            mode="hosted", login_gate="enabled", target="configured"
        )._value.get()  # type: ignore[attr-defined]
        == 1
    )
    assert BUILD_INFO.labels(sha="a" * 40)._value.get() == 1  # type: ignore[attr-defined]
