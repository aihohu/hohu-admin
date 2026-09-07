"""Observe a hosted canary and emit a machine-readable rollback decision."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import subprocess
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from jose import JWTError, jwt

ROLLBACK_EXIT_CODE = 2
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class CanaryObservation:
    health_status: int | None
    auth_status: int | None
    elapsed_ms: int

    @property
    def succeeded(self) -> bool:
        return self.health_status == 200 and self.auth_status == 200


@dataclass(frozen=True, slots=True)
class CanaryThresholds:
    minimum_availability: float = 0.99
    maximum_p95_ms: int = 500
    maximum_consecutive_failures: int = 2

    def __post_init__(self) -> None:
        if not 0 < self.minimum_availability <= 1:
            raise ValueError("minimum availability must be in (0, 1]")
        if self.maximum_p95_ms <= 0 or self.maximum_consecutive_failures < 0:
            raise ValueError("canary thresholds must be non-negative")


def load_canary_token(environ: Mapping[str, str]) -> str:
    """Read the short-lived token only from process environment."""
    token = environ.get("HOHU_TENANT_CANARY_ACCESS_TOKEN", "").strip()
    if not token or any(character.isspace() for character in token):
        raise ValueError("canary access token environment is missing or invalid")
    return token


def load_canary_base_url(environ: Mapping[str, str]) -> str:
    """Bind the credential destination to protected deployment environment."""
    value = environ.get("HOHU_TENANT_CANARY_BASE_URL", "")
    if not value.strip():
        raise ValueError("canary base URL environment is missing")
    return _validated_base_url(value)


def validate_canary_token_identity(token: str, *, target_tenant_id: int) -> None:
    """Fail before I/O unless the opaque credential claims the intended target."""
    if (
        isinstance(target_tenant_id, bool)
        or not isinstance(target_tenant_id, int)
        or target_tenant_id <= 0
    ):
        raise ValueError("target tenant id must be a positive integer")
    try:
        claims = jwt.get_unverified_claims(token)
    except JWTError:
        raise ValueError("canary access token is malformed") from None
    if claims.get("type") != "access" or claims.get("tid") != str(target_tenant_id):
        raise ValueError("canary access token does not match the target tenant")


def _status_counts(values: Sequence[int | None]) -> dict[str, int]:
    counts = Counter(
        "transport_error" if value is None else str(value) for value in values
    )
    return dict(sorted(counts.items()))


def _p95(values: Sequence[int]) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _longest_failure_streak(observations: Sequence[CanaryObservation]) -> int:
    longest = current = 0
    for observation in observations:
        if observation.succeeded:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def build_monitor_report(
    *,
    build_sha: str,
    observations: Sequence[CanaryObservation],
    metrics_contract_ok: bool,
    thresholds: CanaryThresholds,
) -> dict:
    """Project observations to aggregate facts that never contain credentials."""
    normalized_sha = build_sha.strip().lower()
    if _BUILD_SHA_RE.fullmatch(normalized_sha) is None:
        raise ValueError("build SHA must be a full 40-character Git SHA")
    if not observations:
        raise ValueError("at least one canary observation is required")
    successes = sum(observation.succeeded for observation in observations)
    availability = successes / len(observations)
    p95_ms = _p95([observation.elapsed_ms for observation in observations])
    longest_failures = _longest_failure_streak(observations)
    reasons: list[str] = []
    if not metrics_contract_ok:
        reasons.append("CANARY_METRICS_CONTRACT_MISSING")
    if availability < thresholds.minimum_availability:
        reasons.append("CANARY_AVAILABILITY_BELOW_SLO")
    if p95_ms > thresholds.maximum_p95_ms:
        reasons.append("CANARY_LATENCY_ABOVE_SLO")
    if longest_failures > thresholds.maximum_consecutive_failures:
        reasons.append("CANARY_CONSECUTIVE_FAILURES_ABOVE_SLO")
    return {
        "schemaVersion": 1,
        "buildSha": normalized_sha,
        "thresholds": {
            "minimumAvailability": thresholds.minimum_availability,
            "maximumP95Ms": thresholds.maximum_p95_ms,
            "maximumConsecutiveFailures": thresholds.maximum_consecutive_failures,
        },
        "results": {
            "sampleCount": len(observations),
            "successCount": successes,
            "availability": round(availability, 6),
            "p95Ms": p95_ms,
            "longestFailureStreak": longest_failures,
            "metricsContractOk": metrics_contract_ok,
            "healthStatusCounts": _status_counts(
                [observation.health_status for observation in observations]
            ),
            "authStatusCounts": _status_counts(
                [observation.auth_status for observation in observations]
            ),
        },
        "rollbackRequired": bool(reasons),
        "rollbackReasons": sorted(reasons),
        "secretsPersisted": False,
    }


def _validated_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("canary base URL must be an absolute HTTP(S) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("canary base URL must not contain credentials or query data")
    if parsed.path not in {"", "/"}:
        raise ValueError("canary base URL must not contain a path")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("remote canary monitoring requires HTTPS")
    return value.strip().rstrip("/")


def _verified_build_sha(expected: str) -> str:
    normalized_expected = expected.strip().lower()
    if _BUILD_SHA_RE.fullmatch(normalized_expected) is None:
        raise ValueError("build SHA must be a full 40-character Git SHA")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    worktree = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    if worktree.stdout.strip():
        raise ValueError("canary monitor requires a clean Git checkout")
    actual = revision.stdout.strip().lower()
    if actual != normalized_expected:
        raise ValueError("build SHA does not match the checked-out source")
    return actual


async def _collect_observations(
    *,
    base_url: str,
    token: str,
    build_sha: str,
    samples: int,
    interval_seconds: float,
    timeout_seconds: float,
) -> tuple[list[CanaryObservation], bool]:
    observations: list[CanaryObservation] = []
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        try:
            metrics = await client.get("/metrics")
            metrics_text = metrics.text if metrics.status_code == 200 else ""
            metrics_contract_ok = (
                "tenant_hosted_gate_decisions_total" in metrics_text
                and 'mode="hosted"' in metrics_text
                and 'login_gate="enabled"' in metrics_text
                and 'target="configured"' in metrics_text
                and f'hohu_build_info{{sha="{build_sha}"}} 1.0' in metrics_text
            )
        except httpx.HTTPError:
            metrics_contract_ok = False

        for index in range(samples):
            started = time.monotonic()
            health_status: int | None = None
            auth_status: int | None = None
            try:
                health = await client.get("/health")
                health_status = health.status_code
                health_ok = health_status == 200
                if health_ok:
                    try:
                        health_ok = health.json().get("status") == "ok"
                    except (TypeError, ValueError):
                        health_ok = False
                if health_ok:
                    auth = await client.get(
                        "/auth/getUserInfo",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    auth_status = auth.status_code
                    if auth_status == 200:
                        try:
                            if auth.json().get("code") != 200:
                                auth_status = 502
                        except (TypeError, ValueError):
                            auth_status = 502
            except httpx.HTTPError:
                pass
            observations.append(
                CanaryObservation(
                    health_status=health_status,
                    auth_status=auth_status,
                    elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
                )
            )
            if index + 1 < samples:
                await asyncio.sleep(interval_seconds)
    return observations, metrics_contract_ok


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Observe one hosted tenant and signal rollback with exit code 2."
    )
    parser.add_argument("--build-sha", required=True)
    parser.add_argument("--target-tenant-id", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--samples", type=int, default=120)
    parser.add_argument("--interval-seconds", type=float, default=15)
    parser.add_argument("--timeout-seconds", type=float, default=5)
    parser.add_argument("--minimum-availability", type=float, default=0.99)
    parser.add_argument("--maximum-p95-ms", type=int, default=500)
    parser.add_argument("--maximum-consecutive-failures", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    try:
        if not 1 <= arguments.samples <= 7_200:
            raise ValueError("samples must be between 1 and 7200")
        if not 0 <= arguments.interval_seconds <= 3600:
            raise ValueError("interval must be between 0 and 3600 seconds")
        if not 0 < arguments.timeout_seconds <= 60:
            raise ValueError("timeout must be between 0 and 60 seconds")
        build_sha = _verified_build_sha(arguments.build_sha)
        token = load_canary_token(os.environ)
        base_url = load_canary_base_url(os.environ)
        validate_canary_token_identity(
            token,
            target_tenant_id=arguments.target_tenant_id,
        )
        thresholds = CanaryThresholds(
            minimum_availability=arguments.minimum_availability,
            maximum_p95_ms=arguments.maximum_p95_ms,
            maximum_consecutive_failures=arguments.maximum_consecutive_failures,
        )
        observations, metrics_contract_ok = asyncio.run(
            _collect_observations(
                base_url=base_url,
                token=token,
                build_sha=build_sha,
                samples=arguments.samples,
                interval_seconds=arguments.interval_seconds,
                timeout_seconds=arguments.timeout_seconds,
            )
        )
        report = build_monitor_report(
            build_sha=build_sha,
            observations=observations,
            metrics_contract_ok=metrics_contract_ok,
            thresholds=thresholds,
        )
        _write_report(arguments.output, report)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise SystemExit(f"CANARY_MONITOR_INVALID: {error}") from None
    if report["rollbackRequired"]:
        raise SystemExit(ROLLBACK_EXIT_CODE)


if __name__ == "__main__":
    main()
