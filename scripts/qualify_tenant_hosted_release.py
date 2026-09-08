"""Run and attest the isolated hosted-tenant release qualification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

RISK_EXIT_CODE = 2
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_WORKER = _PROJECT_ROOT / "scripts" / "tenant_release_qualification_worker.py"
_PREFLIGHT = _PROJECT_ROOT / "scripts" / "check_tenant_hosted_canary.py"
_MONITOR = _PROJECT_ROOT / "scripts" / "monitor_tenant_hosted_canary.py"
_ISOLATION_AUDIT = _PROJECT_ROOT / "scripts" / "audit_tenant_isolation.py"
_EXPECTED_EVIDENCE = (
    "isolation",
    "preActivation",
    "postActivation",
    "shortObservation",
    "longObservation",
    "rollbackSignal",
)
_EXPECTED_ROLLBACK_REASONS = {
    "CANARY_AVAILABILITY_BELOW_SLO",
    "CANARY_CONSECUTIVE_FAILURES_ABOVE_SLO",
    "CANARY_METRICS_CONTRACT_MISSING",
}
_SENSITIVE_KEYS = {
    "apikey",
    "authorization",
    "cookie",
    "databaseurl",
    "password",
    "rawrequest",
    "rawresponse",
    "refreshtoken",
    "token",
    "accesstoken",
}


@dataclass(frozen=True, slots=True)
class QualificationProfile:
    short_samples: int
    long_samples: int
    interval_seconds: float


PROFILES = {
    "ci": QualificationProfile(
        short_samples=12,
        long_samples=120,
        interval_seconds=0.7,
    ),
    "release": QualificationProfile(
        short_samples=120,
        long_samples=5760,
        interval_seconds=0.7,
    ),
}


class QualificationFailure(Exception):
    """Sanitized orchestration failure safe for reports and CI logs."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _contains_sensitive_field(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            _normalized_key(key) in _SENSITIVE_KEYS or _contains_sensitive_field(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_field(item) for item in value)
    return False


def _status_total(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        for count in value.values()
    ):
        return None
    return sum(value.values())


def _observation_checks(
    *, report: dict, sample_floor: int, prefix: str
) -> list[tuple[str, str, bool]]:
    results = report.get("results")
    thresholds = report.get("thresholds")
    if not isinstance(results, dict) or not isinstance(thresholds, dict):
        return [
            (
                f"{prefix.lower()}_report",
                f"{prefix}_REPORT_INVALID",
                False,
            )
        ]
    sample_count = results.get("sampleCount")
    success_count = results.get("successCount")
    availability = results.get("availability")
    expected_availability = (
        round(success_count / sample_count, 6)
        if isinstance(sample_count, int)
        and not isinstance(sample_count, bool)
        and sample_count > 0
        and isinstance(success_count, int)
        and not isinstance(success_count, bool)
        and 0 <= success_count <= sample_count
        else None
    )
    return [
        (
            f"{prefix.lower()}_sample_floor",
            f"{prefix}_SAMPLE_FLOOR_NOT_MET",
            isinstance(sample_count, int)
            and not isinstance(sample_count, bool)
            and sample_count >= sample_floor,
        ),
        (
            f"{prefix.lower()}_result_consistency",
            f"{prefix}_RESULT_INCONSISTENT",
            expected_availability is not None
            and availability == expected_availability
            and _status_total(results.get("healthStatusCounts")) == sample_count
            and _status_total(results.get("authStatusCounts")) == sample_count,
        ),
        (
            f"{prefix.lower()}_thresholds",
            f"{prefix}_THRESHOLDS_INVALID",
            thresholds
            == {
                "minimumAvailability": 0.99,
                "maximumP95Ms": 500,
                "maximumConsecutiveFailures": 2,
            },
        ),
        (
            f"{prefix.lower()}_slo",
            f"{prefix}_SLO_FAILED",
            report.get("rollbackRequired") is False
            and report.get("rollbackReasons") == []
            and results.get("metricsContractOk") is True
            and isinstance(availability, (int, float))
            and not isinstance(availability, bool)
            and availability >= 0.99
            and isinstance(results.get("p95Ms"), int)
            and results["p95Ms"] <= 500
            and isinstance(results.get("longestFailureStreak"), int)
            and results["longestFailureStreak"] <= 2,
        ),
    ]


def build_qualification_report(
    *,
    build_sha: str,
    profile: str,
    reports: dict[str, dict],
    evidence_hashes: dict[str, str],
    boundary: dict[str, Any],
) -> dict:
    """Validate child evidence and project one identity-free release decision."""
    if profile not in PROFILES:
        raise ValueError("qualification profile is invalid")
    if _BUILD_SHA_RE.fullmatch(build_sha) is None:
        raise ValueError("build SHA must be a full 40-character Git SHA")
    plan = PROFILES[profile]
    failures: set[str] = set()
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, failure_code: str) -> None:
        normalized = bool(passed)
        checks.append({"name": name, "passed": normalized})
        if not normalized:
            failures.add(failure_code)

    check(
        "evidence_set",
        set(reports) == set(_EXPECTED_EVIDENCE)
        and set(evidence_hashes) == set(_EXPECTED_EVIDENCE),
        "EVIDENCE_SET_INVALID",
    )
    check(
        "evidence_hashes",
        all(
            isinstance(evidence_hashes.get(name), str)
            and _HASH_RE.fullmatch(evidence_hashes[name]) is not None
            for name in _EXPECTED_EVIDENCE
        ),
        "EVIDENCE_HASH_INVALID",
    )
    check(
        "evidence_sensitive_fields",
        not _contains_sensitive_field(reports),
        "EVIDENCE_CONTAINS_SENSITIVE_FIELD",
    )
    available_reports = [
        reports.get(name) for name in _EXPECTED_EVIDENCE if name in reports
    ]
    check(
        "evidence_schema",
        len(available_reports) == len(_EXPECTED_EVIDENCE)
        and all(
            isinstance(report, dict) and report.get("schemaVersion") == 1
            for report in available_reports
        ),
        "EVIDENCE_SCHEMA_INVALID",
    )
    check(
        "evidence_source",
        len(available_reports) == len(_EXPECTED_EVIDENCE)
        and all(report.get("buildSha") == build_sha for report in available_reports),
        "EVIDENCE_BUILD_SHA_MISMATCH",
    )
    check(
        "evidence_secret_projection",
        all(
            report.get("secretsPersisted") is False
            for name, report in reports.items()
            if name != "isolation" and isinstance(report, dict)
        ),
        "EVIDENCE_SECRET_PROJECTION_INVALID",
    )

    isolation = reports.get("isolation", {})
    check(
        "tenant_isolation_audit",
        isolation.get("riskCount") == 0 and isolation.get("risks") == [],
        "TENANT_ISOLATION_AUDIT_FAILED",
    )
    pre = reports.get("preActivation", {})
    post = reports.get("postActivation", {})
    pre_target = pre.get("database", {}).get("target", {})
    post_target = post.get("database", {}).get("target", {})
    check(
        "pre_activation",
        pre.get("phase") == "pre_activation"
        and pre.get("riskCount") == 0
        and pre.get("risks") == []
        and pre.get("database", {}).get("nonTargetActiveCount") == 0
        and pre_target.get("status") == "2"
        and pre_target.get("lifecycleState") == "prepared"
        and isinstance(pre_target.get("bootstrapVersion"), int)
        and pre_target["bootstrapVersion"] >= 1,
        "PRE_ACTIVATION_FAILED",
    )
    check(
        "post_activation",
        post.get("phase") == "post_activation"
        and post.get("riskCount") == 0
        and post.get("risks") == []
        and post.get("database", {}).get("nonTargetActiveCount") == 0
        and post_target.get("status") == "1"
        and post_target.get("lifecycleState") == "active"
        and isinstance(post_target.get("bootstrapVersion"), int)
        and post_target["bootstrapVersion"] >= 1,
        "POST_ACTIVATION_FAILED",
    )
    check(
        "pre_post_target",
        isinstance(pre.get("targetTenantId"), str)
        and pre["targetTenantId"].isdigit()
        and int(pre["targetTenantId"]) > 0
        and post.get("targetTenantId") == pre["targetTenantId"],
        "PRE_POST_TARGET_MISMATCH",
    )

    for check_name, failure_code, passed in _observation_checks(
        report=reports.get("shortObservation", {}),
        sample_floor=plan.short_samples,
        prefix="SHORT_OBSERVATION",
    ):
        check(check_name, passed, failure_code)
    for check_name, failure_code, passed in _observation_checks(
        report=reports.get("longObservation", {}),
        sample_floor=plan.long_samples,
        prefix="LONG_OBSERVATION",
    ):
        check(check_name, passed, failure_code)

    rollback = reports.get("rollbackSignal", {})
    rollback_results = rollback.get("results", {})
    rollback_samples = rollback_results.get("sampleCount")
    check(
        "rollback_signal",
        rollback.get("rollbackRequired") is True
        and _EXPECTED_ROLLBACK_REASONS.issubset(
            set(rollback.get("rollbackReasons", []))
        )
        and isinstance(rollback_samples, int)
        and rollback_samples >= 3
        and rollback_results.get("successCount") == 0
        and rollback_results.get("metricsContractOk") is False
        and rollback_results.get("healthStatusCounts") == {"200": rollback_samples}
        and rollback_results.get("authStatusCounts") == {"401": rollback_samples},
        "ROLLBACK_SIGNAL_FAILED",
    )

    check(
        "non_target_activation",
        boundary.get("nonTargetActivationStatus") == 400
        and boundary.get("nonTargetActivationErrorCode")
        == "PLATFORM_TENANT_CANARY_NOT_ALLOWED",
        "NON_TARGET_ACTIVATION_NOT_BLOCKED",
    )
    check(
        "non_target_access",
        boundary.get("nonTargetAccessStatus") == 401
        and boundary.get("nonTargetAccessErrorCode") == "TENANT_HOSTED_ACCESS_DISABLED",
        "NON_TARGET_ACCESS_NOT_BLOCKED",
    )
    check(
        "non_target_side_effects",
        boundary.get("nonTargetStateUnchanged") is True,
        "NON_TARGET_SIDE_EFFECT_DETECTED",
    )
    check(
        "rollback_access",
        boundary.get("rollbackAccessStatus") == 401
        and boundary.get("rollbackAccessErrorCode") == "TENANT_HOSTED_ACCESS_DISABLED"
        and boundary.get("rollbackStateUnchanged") is True,
        "ROLLBACK_ACCESS_NOT_REVOKED",
    )
    check(
        "platform_audit_lineage",
        boundary.get("platformAuditPairCount") == 3,
        "PLATFORM_AUDIT_LINEAGE_INCOMPLETE",
    )
    check(
        "final_state",
        boundary.get("finalActiveNonDefaultCount") == 0
        and boundary.get("targetFinalLifecycleState") == "disabled"
        and boundary.get("nonTargetFinalLifecycleState") == "prepared"
        and boundary.get("redisDbSize") == 0,
        "FINAL_STATE_UNSAFE",
    )

    short_results = reports.get("shortObservation", {}).get("results", {})
    long_results = reports.get("longObservation", {}).get("results", {})
    return {
        "schemaVersion": 1,
        "qualification": "tenant_hosted_release",
        "profile": profile,
        "buildSha": build_sha,
        "qualified": not failures,
        "requiresProductionTenant": False,
        "checks": checks,
        "failureCodes": sorted(failures),
        "evidence": {
            name: {"sha256": evidence_hashes.get(name, "")}
            for name in _EXPECTED_EVIDENCE
        },
        "observations": {
            "short": {
                "sampleCount": short_results.get("sampleCount"),
                "availability": short_results.get("availability"),
                "p95Ms": short_results.get("p95Ms"),
            },
            "long": {
                "sampleCount": long_results.get("sampleCount"),
                "availability": long_results.get("availability"),
                "p95Ms": long_results.get("p95Ms"),
            },
        },
        "boundary": {
            "nonTargetActivationBlocked": boundary.get("nonTargetActivationStatus")
            == 400,
            "nonTargetAccessBlocked": boundary.get("nonTargetAccessStatus") == 401,
            "nonTargetStateUnchanged": boundary.get("nonTargetStateUnchanged"),
            "rollbackAccessRevoked": boundary.get("rollbackAccessStatus") == 401,
            "rollbackStateUnchanged": boundary.get("rollbackStateUnchanged"),
            "platformAuditPairCount": boundary.get("platformAuditPairCount"),
            "activeNonDefaultCount": boundary.get("finalActiveNonDefaultCount"),
            "cacheEntryCount": boundary.get("redisDbSize"),
        },
        "secretsPersisted": False,
    }


def validate_ephemeral_environment(environ: dict[str, str]) -> None:
    """Refuse to mutate any database not explicitly declared disposable."""
    if environ.get("HOHU_RELEASE_QUALIFICATION_EPHEMERAL", "").lower() != "true":
        raise ValueError("explicit ephemeral database acknowledgement is required")
    if environ.get("ENV") != "test":
        raise ValueError("qualification parent must run with ENV=test")
    if environ.get("TENANT_MODE", "single") != "single":
        raise ValueError("qualification parent must start in single tenant mode")
    if environ.get("TENANT_HOSTED_LOGIN_ENABLED", "false").lower() != "false":
        raise ValueError("qualification parent hosted gate must be disabled")
    database_url = environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("an async PostgreSQL qualification database is required")
    if len(environ.get("SECRET_KEY", "")) < 32:
        raise ValueError("a test-only secret key of at least 32 characters is required")
    try:
        redis_db = int(environ.get("REDIS_DB", ""))
    except ValueError:
        redis_db = -1
    if redis_db <= 0:
        raise ValueError("a non-default isolated Redis database is required")


def verify_source_and_output(
    *, expected_sha: str, output: Path, project_root: Path = _PROJECT_ROOT
) -> str:
    """Bind qualification to one clean commit and keep artifacts outside it."""
    normalized_expected = expected_sha.strip().lower()
    if _BUILD_SHA_RE.fullmatch(normalized_expected) is None:
        raise ValueError("build SHA must be a full 40-character Git SHA")
    root = project_root.resolve()
    resolved_output = output.resolve()
    if resolved_output.is_relative_to(root):
        raise ValueError("qualification output must be outside the checkout")
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=True,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Git source verification failed") from None
    if status.stdout.strip():
        raise ValueError("qualification requires a clean Git checkout")
    actual = revision.stdout.strip().lower()
    if actual != normalized_expected:
        raise ValueError("build SHA does not match the checked-out source")
    return actual


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_report(path: Path) -> dict:
    if path.stat().st_size > 5 * 1024 * 1024:
        raise QualificationFailure("EVIDENCE_FILE_TOO_LARGE")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise QualificationFailure("EVIDENCE_FILE_INVALID") from None
    if not isinstance(value, dict):
        raise QualificationFailure("EVIDENCE_FILE_INVALID")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_checked(
    arguments: list[str],
    *,
    environment: dict[str, str],
    failure_code: str,
    allowed_exit_codes: frozenset[int] = frozenset({0}),
    input_text: str | None = None,
    timeout: float = 300,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            arguments,
            cwd=_PROJECT_ROOT,
            env=environment,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise QualificationFailure(failure_code) from None
    if completed.returncode not in allowed_exit_codes:
        raise QualificationFailure(failure_code)
    return completed


def _worker_json(
    stage: str,
    *,
    output: Path,
    environment: dict[str, str],
    extra: list[str] | None = None,
) -> dict:
    _run_checked(
        [sys.executable, str(_WORKER), stage, "--output", str(output), *(extra or [])],
        environment=environment,
        failure_code=f"WORKER_{stage.upper()}_FAILED",
    )
    return _load_report(output)


def _issue_token(
    *,
    tenant_id: int,
    user_id: int,
    tenant_version: int,
    environment: dict[str, str],
) -> str:
    completed = _run_checked(
        [
            sys.executable,
            str(_WORKER),
            "token",
            "--tenant-id",
            str(tenant_id),
            "--user-id",
            str(user_id),
            "--tenant-version",
            str(tenant_version),
        ],
        environment=environment,
        failure_code="TOKEN_ISSUE_FAILED",
    )
    token = completed.stdout.strip()
    if not token or any(character.isspace() for character in token):
        raise QualificationFailure("TOKEN_ISSUE_FAILED")
    return token


def _snapshot(
    *,
    output: Path,
    target_tenant_id: int,
    control_tenant_id: int,
    environment: dict[str, str],
) -> dict:
    return _worker_json(
        "snapshot",
        output=output,
        environment=environment,
        extra=[
            "--target-tenant-id",
            str(target_tenant_id),
            "--control-tenant-id",
            str(control_tenant_id),
        ],
    )


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_api(process: subprocess.Popen, base_url: str) -> None:
    deadline = time.monotonic() + 30
    with httpx.Client(base_url=base_url, timeout=2, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise QualificationFailure("API_START_FAILED")
            try:
                response = client.get("/health")
                if (
                    response.status_code == 200
                    and response.json().get("status") == "ok"
                ):
                    return
            except (httpx.HTTPError, TypeError, ValueError):
                pass
            time.sleep(0.2)
    raise QualificationFailure("API_START_FAILED")


def _start_api(
    *, environment: dict[str, str], port: int, log_path: Path
) -> tuple[subprocess.Popen, Any]:
    log_handle = log_path.open("wb")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
                "--no-access-log",
            ],
            cwd=_PROJECT_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt"
                else 0
            ),
        )
    except OSError:
        log_handle.close()
        raise QualificationFailure("API_START_FAILED") from None
    return process, log_handle


def _stop_api(process: subprocess.Popen | None, log_handle: Any | None) -> None:
    try:
        if process is not None and process.poll() is None:
            if os.name == "nt":
                process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        if log_handle is not None:
            log_handle.close()


@dataclass(slots=True)
class _ApiRuntime:
    process: subprocess.Popen | None = None
    log_handle: Any | None = None

    def stop(self) -> None:
        _stop_api(self.process, self.log_handle)
        self.process = None
        self.log_handle = None


@contextmanager
def _temporary_runtime():
    """Stop Windows API handles before removing their temporary log directory."""
    with tempfile.TemporaryDirectory(prefix="hohu-plan7c-") as temporary:
        runtime = _ApiRuntime()
        try:
            yield Path(temporary), runtime
        finally:
            runtime.stop()


def _final_snapshot_after_shutdown(
    *,
    runtime: _ApiRuntime,
    output: Path,
    target_tenant_id: int,
    control_tenant_id: int,
    environment: dict[str, str],
) -> dict:
    """Evaluate zero-residue state only after graceful API shutdown."""
    runtime.stop()
    return _snapshot(
        output=output,
        target_tenant_id=target_tenant_id,
        control_tenant_id=control_tenant_id,
        environment=environment,
    )


def _response_json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        raise QualificationFailure("HTTP_RESPONSE_INVALID") from None
    if not isinstance(body, dict):
        raise QualificationFailure("HTTP_RESPONSE_INVALID")
    return body


def _platform_login(client: httpx.Client, *, principal_name: str, password: str) -> str:
    response = client.post(
        "/platform/auth/login",
        json={"principalName": principal_name, "password": password},
    )
    body = _response_json(response)
    value = (
        body.get("data", {}).get("token")
        if isinstance(body.get("data"), dict)
        else None
    )
    if (
        response.status_code != 200
        or body.get("code") != 200
        or not isinstance(value, str)
    ):
        raise QualificationFailure("PLATFORM_LOGIN_FAILED")
    return value


def _platform_headers(token: str, correlation_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Platform-Reason": "Run isolated automated release qualification",
        "X-Platform-Ticket": "PLAN7C-AUTOMATED-QUALIFICATION",
        "X-Correlation-ID": correlation_id,
    }


def _run_preflight(
    *, phase: str, path: Path, build_sha: str, environment: dict[str, str]
) -> dict:
    _run_checked(
        [
            sys.executable,
            str(_PREFLIGHT),
            "--build-sha",
            build_sha,
            "--phase",
            phase,
            "--output",
            str(path),
        ],
        environment=environment,
        failure_code=f"{phase.upper()}_COMMAND_FAILED",
    )
    return _load_report(path)


def _run_monitor(
    *,
    path: Path,
    build_sha: str,
    tenant_id: int,
    token: str,
    samples: int,
    interval_seconds: float,
    base_url: str,
    environment: dict[str, str],
    rollback_expected: bool = False,
) -> dict:
    monitor_environment = environment | {
        "HOHU_TENANT_CANARY_BASE_URL": base_url,
        "HOHU_TENANT_CANARY_ACCESS_TOKEN": token,
    }
    timeout = max(300.0, samples * (interval_seconds + 1.0))
    _run_checked(
        [
            sys.executable,
            str(_MONITOR),
            "--build-sha",
            build_sha,
            "--target-tenant-id",
            str(tenant_id),
            "--samples",
            str(samples),
            "--interval-seconds",
            str(interval_seconds),
            "--output",
            str(path),
        ],
        environment=monitor_environment,
        failure_code=(
            "ROLLBACK_MONITOR_DID_NOT_SIGNAL"
            if rollback_expected
            else "CANARY_MONITOR_FAILED"
        ),
        allowed_exit_codes=(frozenset({2}) if rollback_expected else frozenset({0})),
        timeout=timeout,
    )
    return _load_report(path)


def _audit_pair_count(snapshot: dict) -> int:
    expected = {
        "plan7c-qualification-activate-control": 400,
        "plan7c-qualification-activate-target": 200,
        "plan7c-qualification-disable-target": 200,
    }
    pairs = 0
    events = snapshot.get("auditEvents", [])
    for correlation_id, completion_status in expected.items():
        matching = [
            event for event in events if event.get("correlationId") == correlation_id
        ]
        if len(matching) != 2:
            continue
        event_types = {event.get("eventType") for event in matching}
        completed = next(
            (event for event in matching if event.get("eventType") == "completed"),
            None,
        )
        if (
            event_types == {"authorized", "completed"}
            and completed is not None
            and completed.get("statusCode") == completion_status
        ):
            pairs += 1
    return pairs


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run isolated production-profile hosted release qualification."
    )
    parser.add_argument("--build-sha", required=True)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="ci")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--api-port", type=int, default=0)
    return parser.parse_args()


def _base_environment(build_sha: str, temporary_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.setdefault("REDIS_PASSWORD", "")
    environment.update(
        {
            "ENV": "test",
            "APP_ROLE": "api",
            "TENANT_MODE": "single",
            "TENANT_HOSTED_LOGIN_ENABLED": "false",
            "RELEASE_BUILD_SHA": build_sha,
            "ACCESS_TOKEN_EXPIRE_MINUTES": "180",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "UPLOAD_DIR": str(temporary_root / "uploads"),
            "PRIVATE_UPLOAD_DIR": str(temporary_root / "private_uploads"),
        }
    )
    return environment


def _failure_report(*, build_sha: str, profile: str, code: str) -> dict:
    safe_build_sha = build_sha if _BUILD_SHA_RE.fullmatch(build_sha) else ""
    return {
        "schemaVersion": 1,
        "qualification": "tenant_hosted_release",
        "profile": profile,
        "buildSha": safe_build_sha,
        "qualified": False,
        "requiresProductionTenant": False,
        "checks": [],
        "failureCodes": [code],
        "evidence": {},
        "secretsPersisted": False,
    }


def _write_failure_report(
    *, output: Path, build_sha: str, profile: str, code: str
) -> None:
    """Best-effort failure evidence without echoing exception or secret values."""
    try:
        if output.resolve().is_relative_to(_PROJECT_ROOT.resolve()):
            return
        _write_report(
            output,
            _failure_report(build_sha=build_sha, profile=profile, code=code),
        )
    except OSError:
        pass


def run(arguments: argparse.Namespace) -> int:
    build_sha = arguments.build_sha.strip().lower()
    output = arguments.output
    try:
        validate_ephemeral_environment(dict(os.environ))
        build_sha = verify_source_and_output(
            expected_sha=build_sha,
            output=output,
        )
        if arguments.api_port != 0 and not 1024 <= arguments.api_port <= 65535:
            raise QualificationFailure("API_PORT_INVALID")
        profile = PROFILES[arguments.profile]
        artifact_root = output.resolve().parent
        artifact_root.mkdir(parents=True, exist_ok=True)
        evidence_paths = {
            "isolation": artifact_root / "tenant-isolation.json",
            "preActivation": artifact_root / "pre-activation.json",
            "postActivation": artifact_root / "post-activation.json",
            "shortObservation": artifact_root / "short-observation.json",
            "longObservation": artifact_root / "long-observation.json",
            "rollbackSignal": artifact_root / "rollback-signal.json",
        }
        if output.resolve() in {path.resolve() for path in evidence_paths.values()}:
            raise QualificationFailure("OUTPUT_PATH_CONFLICT")

        with _temporary_runtime() as (temporary_root, runtime):
            environment = _base_environment(build_sha, temporary_root)
            platform_password = f"Qp1-{secrets.token_urlsafe(9)}"
            tenant_password = f"Qt1-{secrets.token_urlsafe(9)}"
            secret_environment = environment | {
                "HOHU_RELEASE_QUALIFICATION_PLATFORM_PASSWORD": platform_password,
                "HOHU_RELEASE_QUALIFICATION_TENANT_PASSWORD": tenant_password,
            }

            _run_checked(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                environment=environment,
                failure_code="MIGRATION_FAILED",
            )
            fresh = _worker_json(
                "fresh",
                output=temporary_root / "fresh.json",
                environment=environment,
            )
            if fresh.get("fresh") is not True:
                raise QualificationFailure("DATABASE_NOT_FRESH")
            _run_checked(
                [sys.executable, str(_PROJECT_ROOT / "scripts" / "init_db.py")],
                environment=environment,
                failure_code="DEFAULT_SEED_FAILED",
                input_text=f"{tenant_password}\n",
            )
            fixture = _worker_json(
                "seed",
                output=temporary_root / "fixture.json",
                environment=secret_environment,
            )
            target = fixture["target"]
            control = fixture["control"]
            target_id = int(target["tenantId"])
            control_id = int(control["tenantId"])

            _run_checked(
                [
                    sys.executable,
                    str(_ISOLATION_AUDIT),
                    "--build-sha",
                    build_sha,
                    "--output",
                    str(evidence_paths["isolation"]),
                ],
                environment=environment,
                failure_code="TENANT_ISOLATION_AUDIT_COMMAND_FAILED",
            )
            hosted_environment = environment | {
                "ENV": "prod",
                "TENANT_MODE": "hosted",
                "TENANT_HOSTED_LOGIN_ENABLED": "true",
                "TENANT_HOSTED_CANARY_TENANT_ID": str(target_id),
            }
            reports = {
                "isolation": _load_report(evidence_paths["isolation"]),
                "preActivation": _run_preflight(
                    phase="pre_activation",
                    path=evidence_paths["preActivation"],
                    build_sha=build_sha,
                    environment=hosted_environment,
                ),
            }

            port = arguments.api_port or _available_port()
            base_url = f"http://127.0.0.1:{port}"
            runtime.process, runtime.log_handle = _start_api(
                environment=hosted_environment,
                port=port,
                log_path=temporary_root / "hosted-api.log",
            )
            _wait_for_api(runtime.process, base_url)
            with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as client:
                platform_token = _platform_login(
                    client,
                    principal_name=fixture["principalName"],
                    password=platform_password,
                )
                control_activation = client.post(
                    f"/platform/tenants/{control_id}/activate",
                    headers=_platform_headers(
                        platform_token,
                        "plan7c-qualification-activate-control",
                    ),
                )
                control_activation_body = _response_json(control_activation)
                target_activation = client.post(
                    f"/platform/tenants/{target_id}/activate",
                    headers=_platform_headers(
                        platform_token,
                        "plan7c-qualification-activate-target",
                    ),
                )
                target_activation_body = _response_json(target_activation)
                if (
                    target_activation.status_code != 200
                    or target_activation_body.get("code") != 200
                ):
                    raise QualificationFailure("TARGET_ACTIVATION_FAILED")

                reports["postActivation"] = _run_preflight(
                    phase="post_activation",
                    path=evidence_paths["postActivation"],
                    build_sha=build_sha,
                    environment=hosted_environment,
                )
                before_control = _snapshot(
                    output=temporary_root / "before-control.json",
                    target_tenant_id=target_id,
                    control_tenant_id=control_id,
                    environment=hosted_environment,
                )
                control_access_token = _issue_token(
                    tenant_id=control_id,
                    user_id=int(control["userId"]),
                    tenant_version=int(control["tenantVersion"]),
                    environment=hosted_environment,
                )
                control_access = client.get(
                    "/auth/getUserInfo",
                    headers={"Authorization": f"Bearer {control_access_token}"},
                )
                control_access_body = _response_json(control_access)
                after_control = _snapshot(
                    output=temporary_root / "after-control.json",
                    target_tenant_id=target_id,
                    control_tenant_id=control_id,
                    environment=hosted_environment,
                )
                tenant_login = client.post(
                    "/auth/login",
                    json={
                        "loginType": "password",
                        "tenantCode": target["tenantCode"],
                        "userName": "admin",
                        "password": tenant_password,
                    },
                )
                tenant_login_body = _response_json(tenant_login)
                tenant_access_token = (
                    tenant_login_body.get("data", {}).get("token")
                    if isinstance(tenant_login_body.get("data"), dict)
                    else None
                )
                if (
                    tenant_login.status_code != 200
                    or tenant_login_body.get("code") != 200
                    or not isinstance(tenant_access_token, str)
                ):
                    raise QualificationFailure("TARGET_LOGIN_FAILED")

            reports["shortObservation"] = _run_monitor(
                path=evidence_paths["shortObservation"],
                build_sha=build_sha,
                tenant_id=target_id,
                token=tenant_access_token,
                samples=profile.short_samples,
                interval_seconds=profile.interval_seconds,
                base_url=base_url,
                environment=hosted_environment,
            )
            reports["longObservation"] = _run_monitor(
                path=evidence_paths["longObservation"],
                build_sha=build_sha,
                tenant_id=target_id,
                token=tenant_access_token,
                samples=profile.long_samples,
                interval_seconds=profile.interval_seconds,
                base_url=base_url,
                environment=hosted_environment,
            )
            runtime.stop()

            single_environment = environment | {
                "ENV": "prod",
                "TENANT_MODE": "single",
                "TENANT_HOSTED_LOGIN_ENABLED": "false",
            }
            runtime.process, runtime.log_handle = _start_api(
                environment=single_environment,
                port=port,
                log_path=temporary_root / "single-api.log",
            )
            _wait_for_api(runtime.process, base_url)
            rollback_before = _snapshot(
                output=temporary_root / "rollback-before.json",
                target_tenant_id=target_id,
                control_tenant_id=control_id,
                environment=single_environment,
            )
            with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as client:
                rollback_access = client.get(
                    "/auth/getUserInfo",
                    headers={"Authorization": f"Bearer {tenant_access_token}"},
                )
                rollback_access_body = _response_json(rollback_access)
            rollback_after_access = _snapshot(
                output=temporary_root / "rollback-after-access.json",
                target_tenant_id=target_id,
                control_tenant_id=control_id,
                environment=single_environment,
            )
            reports["rollbackSignal"] = _run_monitor(
                path=evidence_paths["rollbackSignal"],
                build_sha=build_sha,
                tenant_id=target_id,
                token=tenant_access_token,
                samples=3,
                interval_seconds=0,
                base_url=base_url,
                environment=single_environment,
                rollback_expected=True,
            )
            rollback_after_monitor = _snapshot(
                output=temporary_root / "rollback-after-monitor.json",
                target_tenant_id=target_id,
                control_tenant_id=control_id,
                environment=single_environment,
            )
            with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as client:
                platform_token = _platform_login(
                    client,
                    principal_name=fixture["principalName"],
                    password=platform_password,
                )
                disable_response = client.post(
                    f"/platform/tenants/{target_id}/disable",
                    headers=_platform_headers(
                        platform_token,
                        "plan7c-qualification-disable-target",
                    ),
                )
                disable_body = _response_json(disable_response)
                if (
                    disable_response.status_code != 200
                    or disable_body.get("code") != 200
                ):
                    raise QualificationFailure("TARGET_DISABLE_FAILED")
            final_state = _final_snapshot_after_shutdown(
                runtime=runtime,
                output=temporary_root / "final-state.json",
                target_tenant_id=target_id,
                control_tenant_id=control_id,
                environment=single_environment,
            )

            boundary = {
                "nonTargetActivationStatus": control_activation.status_code,
                "nonTargetActivationErrorCode": control_activation_body.get(
                    "errorCode"
                ),
                "nonTargetAccessStatus": control_access.status_code,
                "nonTargetAccessErrorCode": control_access_body.get("errorCode"),
                "nonTargetStateUnchanged": before_control.get("control")
                == after_control.get("control")
                and before_control.get("redisDbSize")
                == after_control.get("redisDbSize"),
                "rollbackAccessStatus": rollback_access.status_code,
                "rollbackAccessErrorCode": rollback_access_body.get("errorCode"),
                "rollbackStateUnchanged": rollback_before.get("target")
                == rollback_after_access.get("target")
                == rollback_after_monitor.get("target")
                and rollback_before.get("redisDbSize")
                == rollback_after_access.get("redisDbSize")
                == rollback_after_monitor.get("redisDbSize"),
                "platformAuditPairCount": _audit_pair_count(final_state),
                "finalActiveNonDefaultCount": final_state.get("activeNonDefaultCount"),
                "targetFinalLifecycleState": final_state.get("target", {}).get(
                    "lifecycleState"
                ),
                "nonTargetFinalLifecycleState": final_state.get("control", {}).get(
                    "lifecycleState"
                ),
                "redisDbSize": final_state.get("redisDbSize"),
            }
            evidence_hashes = {
                name: _sha256(path) for name, path in evidence_paths.items()
            }
            report = build_qualification_report(
                build_sha=build_sha,
                profile=arguments.profile,
                reports=reports,
                evidence_hashes=evidence_hashes,
                boundary=boundary,
            )
            _write_report(output, report)
            return 0 if report["qualified"] else RISK_EXIT_CODE
    except (QualificationFailure, ValueError, KeyError) as error:
        code = (
            error.code
            if isinstance(error, QualificationFailure)
            else "QUALIFICATION_INPUT_INVALID"
        )
        _write_failure_report(
            output=output,
            build_sha=build_sha,
            profile=arguments.profile,
            code=code,
        )
        return RISK_EXIT_CODE
    except Exception:
        _write_failure_report(
            output=output,
            build_sha=build_sha,
            profile=arguments.profile,
            code="QUALIFICATION_UNEXPECTED_FAILURE",
        )
        return RISK_EXIT_CODE


def main() -> None:
    raise SystemExit(run(_arguments()))


if __name__ == "__main__":
    main()
