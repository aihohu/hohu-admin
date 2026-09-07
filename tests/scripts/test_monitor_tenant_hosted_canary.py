import json
import time

import pytest
from jose import jwt

from scripts.monitor_tenant_hosted_canary import (
    CanaryObservation,
    CanaryThresholds,
    _validated_base_url,
    build_monitor_report,
    load_canary_base_url,
    load_canary_token,
    validate_canary_token_identity,
)


def test_monitor_report_accepts_a_healthy_window() -> None:
    observations = [
        CanaryObservation(health_status=200, auth_status=200, elapsed_ms=120)
        for _ in range(100)
    ]

    report = build_monitor_report(
        build_sha="a" * 40,
        observations=observations,
        metrics_contract_ok=True,
        thresholds=CanaryThresholds(),
    )

    assert report["rollbackRequired"] is False
    assert report["results"]["availability"] == 1.0
    assert report["results"]["p95Ms"] == 120


def test_monitor_report_signals_rollback_for_slo_and_consecutive_failures() -> None:
    observations = [
        CanaryObservation(health_status=200, auth_status=200, elapsed_ms=100),
        CanaryObservation(health_status=503, auth_status=None, elapsed_ms=700),
        CanaryObservation(health_status=503, auth_status=None, elapsed_ms=710),
        CanaryObservation(health_status=503, auth_status=None, elapsed_ms=720),
    ]

    report = build_monitor_report(
        build_sha="b" * 40,
        observations=observations,
        metrics_contract_ok=True,
        thresholds=CanaryThresholds(),
    )

    assert report["rollbackRequired"] is True
    assert set(report["rollbackReasons"]) == {
        "CANARY_AVAILABILITY_BELOW_SLO",
        "CANARY_LATENCY_ABOVE_SLO",
        "CANARY_CONSECUTIVE_FAILURES_ABOVE_SLO",
    }


def test_monitor_token_comes_from_environment_and_never_enters_report() -> None:
    sentinel = "secret-canary-token"
    assert load_canary_token({"HOHU_TENANT_CANARY_ACCESS_TOKEN": sentinel}) == sentinel

    report = build_monitor_report(
        build_sha="c" * 40,
        observations=[
            CanaryObservation(health_status=200, auth_status=200, elapsed_ms=10)
        ],
        metrics_contract_ok=True,
        thresholds=CanaryThresholds(),
    )

    assert sentinel not in json.dumps(report)


def test_monitor_base_url_comes_from_environment() -> None:
    assert (
        load_canary_base_url(
            {"HOHU_TENANT_CANARY_BASE_URL": "https://canary.example.com/"}
        )
        == "https://canary.example.com"
    )
    with pytest.raises(ValueError, match="environment is missing"):
        load_canary_base_url({})


def test_monitor_token_must_claim_the_exact_canary_tenant() -> None:
    token = jwt.encode(
        {
            "sub": "101",
            "tid": "22",
            "type": "access",
            "exp": int(time.time()) + 60,
        },
        "test-key",
    )

    validate_canary_token_identity(token, target_tenant_id=22)

    with pytest.raises(ValueError, match="does not match"):
        validate_canary_token_identity(token, target_tenant_id=23)


@pytest.mark.parametrize("token", ["not-a-jwt", "a.b.c"])
def test_monitor_rejects_a_malformed_token(token: str) -> None:
    with pytest.raises(ValueError, match="malformed"):
        validate_canary_token_identity(token, target_tenant_id=22)


@pytest.mark.parametrize(
    "url",
    [
        "http://canary.example.com",
        "https://user:secret@canary.example.com",
        "https://canary.example.com/path",
        "https://canary.example.com?token=secret",
    ],
)
def test_monitor_rejects_unsafe_canary_origins(url: str) -> None:
    with pytest.raises(ValueError):
        _validated_base_url(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://canary.example.com/", "https://canary.example.com"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
    ],
)
def test_monitor_accepts_https_and_loopback_origins(url: str, expected: str) -> None:
    assert _validated_base_url(url) == expected
