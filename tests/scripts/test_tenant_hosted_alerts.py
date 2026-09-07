from pathlib import Path

_ALERTS = (
    Path(__file__).resolve().parents[2]
    / "monitoring"
    / "tenant-hosted-canary-alerts.yml"
)


def test_hosted_canary_alerts_cover_configuration_and_authority_boundaries() -> None:
    content = _ALERTS.read_text(encoding="utf-8")

    assert "HostedCanaryConfigurationMissing" in content
    assert "HostedCanaryUnexpectedActivationAttempt" in content
    assert "HostedCanaryNonTargetAuthorityObserved" in content
    assert "HostedCanaryLoginDenialsHigh" in content


def test_hosted_canary_metrics_never_use_identity_labels() -> None:
    content = _ALERTS.read_text(encoding="utf-8")
    hosted_section = content[content.index("name: tenant_hosted_canary") :]

    for prohibited_label in ("tenant_id=", "tenant_code=", "user_id=", "token="):
        assert prohibited_label not in hosted_section
