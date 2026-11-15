"""Fixed diagnostics must never publish model or business-field strings."""

from types import SimpleNamespace

import pytest
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app import main
from app.modules.ai import metrics
from app.modules.ai.agents.gateway.executor import execute_tool
from app.modules.system.field_policy.delivery_catalog import TOOL_OBJECTS


@pytest.fixture
def diagnostic_registry(monkeypatch):
    registry = CollectorRegistry()
    for name, factory in (
        ("TOOL_CALLS_TOTAL", Counter),
        ("TOOL_CALL_DURATION_SECONDS", Histogram),
        ("HITL_PENDING_COUNT", Gauge),
        ("HITL_WAKE_TOTAL", Counter),
        ("HITL_PUBSUB_LOST_TOTAL", Counter),
        ("HITL_TIMEOUT_TOTAL", Counter),
        ("QUOTA_REJECTED_TOTAL", Counter),
        ("SECURITY_EVENTS_TOTAL", Counter),
    ):
        original = getattr(metrics, name)
        monkeypatch.setattr(
            metrics,
            name,
            factory(
                original._name,
                "Isolated fixed diagnostic",
                original._labelnames,
                registry=registry,
            ),
        )
    monkeypatch.setattr(main, "generate_latest", lambda: generate_latest(registry))
    return registry


async def test_unknown_gateway_name_is_not_a_public_metric(diagnostic_registry):
    marker = "private-finance-email@example.invalid"
    result = await execute_tool(
        marker, {}, SimpleNamespace(user=SimpleNamespace(user_id=101))
    )
    assert result.ok is False
    assert result.error_code == "AI_TOOL_NOT_FOUND"
    response = await main.metrics()
    assert marker.encode() not in response.body
    assert (
        diagnostic_registry.get_sample_value(
            "ai_tool_calls_total",
            {
                "tool": "unknown",
                "status": "not_found",
                "risk": "unknown",
                "execution_mode": "unknown",
            },
        )
        == 1
    )


@pytest.mark.parametrize("field", ["tool", "status", "risk", "execution_mode"])
def test_tool_metric_values_are_a_closed_diagnostic_domain(diagnostic_registry, field):
    marker = "private-business-value-" + field
    labels = {
        "tool": "user.list",
        "status": "success",
        "risk": "low",
        "execution_mode": "autonomous",
    }
    labels[field] = marker
    metrics.record_tool_call(**labels, duration_sec=0.1)
    assert marker.encode() not in generate_latest(diagnostic_registry)
    labels[field] = "unknown"
    assert diagnostic_registry.get_sample_value("ai_tool_calls_total", labels) == 1


@pytest.mark.parametrize(
    "invoke",
    [
        lambda value: metrics.record_hitl_created(value),
        lambda value: metrics.record_hitl_resolved(value),
        lambda value: metrics.record_hitl_timeout(value),
        lambda value: metrics.record_hitl_wake(value, "success"),
        lambda value: metrics.record_hitl_wake("memory", value),
        lambda value: metrics.record_quota_rejected(value),
        lambda value: metrics.record_security_event(value),
    ],
)
def test_other_diagnostics_do_not_publish_free_labels(diagnostic_registry, invoke):
    marker = "confidential-field-copy"
    invoke(marker)
    output = generate_latest(diagnostic_registry)
    assert marker.encode() not in output
    assert b'="unknown"' in output


def test_registered_tool_counts_and_histograms_remain_available(diagnostic_registry):
    for name in TOOL_OBJECTS:
        metrics.record_tool_call(name, "success", "low", "autonomous", 0.25)
        assert (
            diagnostic_registry.get_sample_value(
                "ai_tool_calls_total",
                {
                    "tool": name,
                    "status": "success",
                    "risk": "low",
                    "execution_mode": "autonomous",
                },
            )
            == 1
        )
        assert (
            diagnostic_registry.get_sample_value(
                "ai_tool_call_duration_seconds_count", {"tool": name}
            )
            == 1
        )


def test_invalid_label_objects_are_not_stringified(diagnostic_registry):
    class PrivateObject:
        def __str__(self):
            pytest.fail("Private object must not be converted to public text")

    metrics.record_tool_call(PrivateObject(), "success", "low", "autonomous", 0.1)
    assert (
        diagnostic_registry.get_sample_value(
            "ai_tool_call_duration_seconds_count", {"tool": "unknown"}
        )
        == 1
    )


def test_unknown_tool_series_do_not_grow_per_value(diagnostic_registry):
    for value in range(100):
        metrics.record_tool_call(
            "private-input-" + str(value), "not_found", "unknown", "unknown", 0.1
        )
    samples = [
        sample
        for family in diagnostic_registry.collect()
        for sample in family.samples
        if sample.name == "ai_tool_calls_total"
    ]
    assert len(samples) == 1
    assert samples[0].labels["tool"] == "unknown"
    assert samples[0].value == 100


@pytest.mark.usefixtures("diagnostic_registry")
async def test_metrics_response_is_not_cached():
    response = await main.metrics()
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("x-content-type-options") == "nosniff"


async def test_oversized_metrics_fail_without_partial_data(monkeypatch):
    body = b"private-overflow-value\n" * (8 * 1024 * 1024 // 23 + 2)
    assert len(body) > 8 * 1024 * 1024
    monkeypatch.setattr(main, "generate_latest", lambda: body)
    response = await main.metrics()
    assert response.status_code == 503
    assert response.body == b"metrics unavailable"
    assert response.headers.get("cache-control") == "no-store"
