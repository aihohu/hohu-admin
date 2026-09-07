"""Low-cardinality observability for the hosted tenant rollout gate."""

from typing import Literal

from prometheus_client import Counter, Gauge

HostedGateSurface = Literal["login", "access", "refresh", "worker", "activation"]
HostedGateResult = Literal["default", "allowed", "blocked"]

_SURFACES = frozenset({"login", "access", "refresh", "worker", "activation"})
_RESULTS = frozenset({"default", "allowed", "blocked"})

HOSTED_GATE_DECISIONS_TOTAL = Counter(
    "tenant_hosted_gate_decisions_total",
    "Hosted tenant rollout gate decisions",
    ["surface", "result"],
)

HOSTED_ROLLOUT_INFO = Gauge(
    "tenant_hosted_rollout_info",
    "Current hosted tenant rollout configuration",
    ["mode", "login_gate", "target"],
)

BUILD_INFO = Gauge(
    "hohu_build_info",
    "Immutable application source identity",
    ["sha"],
)


def record_hosted_gate_decision(
    *, surface: HostedGateSurface, result: HostedGateResult
) -> None:
    """Record one decision without accepting caller-controlled label values."""
    if surface not in _SURFACES or result not in _RESULTS:
        raise ValueError("invalid hosted rollout metric label")
    HOSTED_GATE_DECISIONS_TOTAL.labels(surface=surface, result=result).inc()


def configure_hosted_rollout_info(
    *,
    mode: Literal["single", "hosted"],
    login_gate: bool,
    target_configured: bool,
    build_sha: str = "",
) -> None:
    """Publish one process-level configuration series without tenant identity."""
    HOSTED_ROLLOUT_INFO.clear()
    HOSTED_ROLLOUT_INFO.labels(
        mode=mode,
        login_gate="enabled" if login_gate else "disabled",
        target="configured" if target_configured else "unconfigured",
    ).set(1)
    BUILD_INFO.clear()
    BUILD_INFO.labels(sha=build_sha or "unbound").set(1)
