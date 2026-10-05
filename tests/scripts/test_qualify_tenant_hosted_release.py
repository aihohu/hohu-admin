import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tests.release import qualify_tenant_hosted_release as qualification
from tests.release import tenant_release_qualification_worker as worker

BUILD_SHA = "a" * 40


def _preflight(phase: str) -> dict:
    active = phase == "post_activation"
    return {
        "schemaVersion": 1,
        "buildSha": BUILD_SHA,
        "phase": phase,
        "targetTenantId": "22",
        "database": {
            "target": {
                "status": "1" if active else "2",
                "lifecycleState": "active" if active else "prepared",
                "bootstrapVersion": 1,
                "rowVersion": 3 if active else 2,
            },
            "nonTargetActiveCount": 0,
        },
        "riskCount": 0,
        "risks": [],
        "secretsPersisted": False,
    }


def _observation(samples: int) -> dict:
    return {
        "schemaVersion": 1,
        "buildSha": BUILD_SHA,
        "thresholds": {
            "minimumAvailability": 0.99,
            "maximumP95Ms": 500,
            "maximumConsecutiveFailures": 2,
        },
        "results": {
            "sampleCount": samples,
            "successCount": samples,
            "availability": 1.0,
            "p95Ms": 25,
            "longestFailureStreak": 0,
            "metricsContractOk": True,
            "healthStatusCounts": {"200": samples},
            "authStatusCounts": {"200": samples},
        },
        "rollbackRequired": False,
        "rollbackReasons": [],
        "secretsPersisted": False,
    }


def _rollback() -> dict:
    return {
        "schemaVersion": 1,
        "buildSha": BUILD_SHA,
        "results": {
            "sampleCount": 3,
            "successCount": 0,
            "availability": 0.0,
            "p95Ms": 5,
            "longestFailureStreak": 3,
            "metricsContractOk": False,
            "healthStatusCounts": {"200": 3},
            "authStatusCounts": {"401": 3},
        },
        "rollbackRequired": True,
        "rollbackReasons": [
            "CANARY_AVAILABILITY_BELOW_SLO",
            "CANARY_CONSECUTIVE_FAILURES_ABOVE_SLO",
            "CANARY_METRICS_CONTRACT_MISSING",
        ],
        "secretsPersisted": False,
    }


def _reports(profile: str = "ci") -> dict[str, dict]:
    plan = qualification.PROFILES[profile]
    return {
        "isolation": {
            "schemaVersion": 1,
            "buildSha": BUILD_SHA,
            "riskCount": 0,
            "risks": [],
        },
        "preActivation": _preflight("pre_activation"),
        "postActivation": _preflight("post_activation"),
        "shortObservation": _observation(plan.short_samples),
        "longObservation": _observation(plan.long_samples),
        "rollbackSignal": _rollback(),
    }


def _boundary() -> dict:
    return {
        "controlActivationStatus": 200,
        "nonTargetActivationStatus": 403,
        "nonTargetActivationErrorCode": "SYSTEM_ADMIN_ONLY",
        "nonTargetAccessStatus": 200,
        "controlDisabledAccessStatus": 401,
        "controlDisabledAccessErrorCode": "TOKEN_EXPIRED",
        "nonTargetStateUnchanged": True,
        "rollbackAccessStatus": 401,
        "rollbackAccessErrorCode": "TENANT_HOSTED_ACCESS_DISABLED",
        "rollbackStateUnchanged": True,
        "platformAuditPairCount": 4,
        "finalActiveNonDefaultCount": 0,
        "targetFinalLifecycleState": "disabled",
        "nonTargetFinalLifecycleState": "disabled",
        "redisDbSize": 0,
    }


def _hashes() -> dict[str, str]:
    return {name: f"{index:x}" * 64 for index, name in enumerate(_reports(), 1)}


def test_management_login_uses_default_tenant_user_session() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path != "/auth/login":
            return httpx.Response(401, json={"code": 401})
        return httpx.Response(200, json={"code": 200, "data": {"token": "session"}})

    with httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(handle)
    ) as client:
        token = qualification._tenant_login(
            client,
            username="renamed-admin",
            password="test-only-password",
            failure_code="SYSTEM_ADMIN_LOGIN_FAILED",
        )

    assert token == "session"
    assert json.loads(requests[0].content) == {
        "loginType": "password",
        "userName": "renamed-admin",
        "password": "test-only-password",
    }


def test_http_failure_logs_status_and_code_without_response_secrets(caplog) -> None:
    response = httpx.Response(
        401,
        json={
            "code": 401,
            "errorCode": "TOKEN_EXPIRED",
            "msg": "private-host:secret",
            "data": {"token": "private-token"},
        },
    )
    with pytest.raises(qualification.QualificationFailure, match="ACTIVATION_FAILED"):
        qualification._require_success(response, failure_code="ACTIVATION_FAILED")
    assert "401" in caplog.text
    assert "TOKEN_EXPIRED" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize(
    "updates,expected",
    [
        ({"nonTargetActivationStatus": 200}, "NON_TARGET_ACTIVATION_NOT_BLOCKED"),
        ({"controlActivationStatus": 400}, "CONTROL_ACTIVATION_FAILED"),
        ({"nonTargetAccessStatus": 403}, "CONTROL_ACCESS_FAILED"),
        ({"controlDisabledAccessStatus": 200}, "CONTROL_DISABLE_ACCESS_NOT_REVOKED"),
    ],
)
def test_current_tenant_authority_boundaries_fail_closed(updates, expected) -> None:
    boundary = _boundary() | updates
    result = qualification.build_qualification_report(
        build_sha=BUILD_SHA,
        profile="ci",
        reports=_reports(),
        evidence_hashes=_hashes(),
        boundary=boundary,
    )
    assert result["qualified"] is False
    assert expected in result["failureCodes"]


def _audit_events() -> list[dict]:
    events = []
    for index, (suffix, target) in enumerate(
        [
            ("activate-target", "target"),
            ("activate-control", "control"),
            ("disable-control", "control"),
            ("disable-target", "target"),
        ],
        start=1,
    ):
        common = {
            "correlationId": f"plan7c-qualification-{suffix}",
            "targetKind": target,
            "auditScope": "platform",
            "method": "POST",
        }
        events.extend(
            [
                common | {"eventType": "authorized", "auditId": str(index)},
                common
                | {
                    "eventType": "completed",
                    "statusCode": 200,
                    "authorizationAuditId": str(index),
                },
            ]
        )
    return events


def test_system_audit_requires_exact_authorization_completion_lineage() -> None:
    events = _audit_events()
    assert qualification._audit_pair_count({"auditEvents": events}) == 4
    events[1]["authorizationAuditId"] = "wrong-authorization"
    assert qualification._audit_pair_count({"auditEvents": events}) == 3
    events = _audit_events()
    events.append(events[1].copy())
    assert qualification._audit_pair_count({"auditEvents": events}) == 3
    events = _audit_events()
    events[1]["auditScope"] = "tenant"
    assert qualification._audit_pair_count({"auditEvents": events}) == 3


def test_worker_projects_system_audit_without_identity_or_request_payload() -> None:
    event = SimpleNamespace(
        operation_log_id=17,
        audit_scope="platform",
        action="completed",
        status_code=200,
        method="POST",
        request_params=json.dumps(
            {
                "correlation_id": "plan7c-qualification-activate-target",
                "authorization_audit_id": 16,
                "target_tenant_id": 22,
                "reason": "private reason",
                "changes": {"secret": "private-value"},
            }
        ),
    )
    result = worker._project_system_audit(
        event, target_tenant_id=22, control_tenant_id=33
    )
    assert result["authorizationAuditId"] == "16"
    assert result["auditId"] == "17"
    assert result["targetKind"] == "target"
    assert "private" not in json.dumps(result)
    event.request_params = "invalid json"
    assert (
        worker._project_system_audit(event, target_tenant_id=22, control_tenant_id=33)
        is None
    )


async def test_cache_cleanup_preserves_unknown_and_persistent_keys(
    monkeypatch, tmp_path: Path
) -> None:
    throttle = "request-limit:api:" + "a" * 64
    settings_key = "cache:tenant:0:setting:request_limits"
    persistent = "request-limit:login:" + "b" * 64
    unknown = "tenant:22:unexpected-state"

    async def scan_iter():
        for key in (throttle, settings_key, persistent, unknown):
            yield key

    client = MagicMock()
    client.scan_iter.side_effect = scan_iter
    client.ttl = AsyncMock(side_effect=[45, 25, -1])
    client.delete = AsyncMock(return_value=1)
    client.dbsize = AsyncMock(return_value=2)
    monkeypatch.setattr(worker, "redis_client", client)
    monkeypatch.setenv("HOHU_RELEASE_QUALIFICATION_EPHEMERAL", "true")
    monkeypatch.setattr(worker.settings, "ENV", "test")
    monkeypatch.setattr(worker.settings, "REDIS_DB", 9)
    output = tmp_path / "cleanup.json"
    await worker._cleanup_cache(output)
    assert [call.args[0] for call in client.delete.await_args_list] == [
        throttle,
        settings_key,
    ]
    assert json.loads(output.read_text(encoding="utf-8"))["redisDbSize"] == 2


@pytest.mark.parametrize(
    "ephemeral,env,redis_db",
    [
        ("false", "test", 9),
        ("true", "prod", 9),
        ("true", "test", 0),
    ],
)
async def test_cache_cleanup_refuses_non_disposable_environment(
    monkeypatch, tmp_path: Path, ephemeral, env, redis_db
) -> None:
    client = MagicMock()
    monkeypatch.setattr(worker, "redis_client", client)
    monkeypatch.setenv("HOHU_RELEASE_QUALIFICATION_EPHEMERAL", ephemeral)
    monkeypatch.setattr(worker.settings, "ENV", env)
    monkeypatch.setattr(worker.settings, "REDIS_DB", redis_db)
    with pytest.raises(ValueError, match="ephemeral"):
        await worker._cleanup_cache(tmp_path / "cleanup.json")
    client.scan_iter.assert_not_called()


def test_healthy_ci_evidence_qualifies_without_identity_or_secret_projection() -> None:
    report = qualification.build_qualification_report(
        build_sha=BUILD_SHA,
        profile="ci",
        reports=_reports(),
        evidence_hashes=_hashes(),
        boundary=_boundary(),
    )

    assert report["qualified"] is True
    assert report["failureCodes"] == []
    assert report["requiresProductionTenant"] is False
    assert report["secretsPersisted"] is False
    serialized = json.dumps(report)
    assert "targetTenantId" not in serialized
    assert "password" not in serialized.lower()
    assert "token" not in serialized.lower()
    assert all(
        "failed" not in item["name"] and "not_met" not in item["name"]
        for item in report["checks"]
    )


@pytest.mark.parametrize("profile", ["ci", "release"])
def test_profiles_enforce_their_declared_sample_floors(profile: str) -> None:
    reports = _reports(profile)
    reports["longObservation"]["results"]["sampleCount"] -= 1
    reports["longObservation"]["results"]["successCount"] -= 1

    result = qualification.build_qualification_report(
        build_sha=BUILD_SHA,
        profile=profile,
        reports=reports,
        evidence_hashes={
            name: f"{index:x}" * 64 for index, name in enumerate(reports, 1)
        },
        boundary=_boundary(),
    )

    assert result["qualified"] is False
    assert "LONG_OBSERVATION_SAMPLE_FLOOR_NOT_MET" in result["failureCodes"]


def test_qualification_fails_closed_for_source_boundary_and_sensitive_evidence() -> (
    None
):
    reports = _reports()
    reports["preActivation"]["buildSha"] = "b" * 40
    reports["postActivation"]["accessToken"] = "must-never-persist"
    boundary = _boundary()
    boundary["nonTargetStateUnchanged"] = False

    result = qualification.build_qualification_report(
        build_sha=BUILD_SHA,
        profile="ci",
        reports=reports,
        evidence_hashes=_hashes(),
        boundary=boundary,
    )

    assert result["qualified"] is False
    assert {
        "EVIDENCE_BUILD_SHA_MISMATCH",
        "EVIDENCE_CONTAINS_SENSITIVE_FIELD",
        "NON_TARGET_SIDE_EFFECT_DETECTED",
    }.issubset(result["failureCodes"])


def test_final_cache_boundary_rejects_residue() -> None:
    boundary = _boundary()
    boundary["redisDbSize"] = 1

    result = qualification.build_qualification_report(
        build_sha=BUILD_SHA,
        profile="ci",
        reports=_reports(),
        evidence_hashes=_hashes(),
        boundary=boundary,
    )

    assert result["qualified"] is False
    assert "FINAL_STATE_UNSAFE" in result["failureCodes"]


@pytest.mark.parametrize(
    "updates",
    [
        {},
        {"HOHU_RELEASE_QUALIFICATION_EPHEMERAL": "false"},
        {"ENV": "prod"},
        {"TENANT_MODE": "hosted"},
        {"TENANT_HOSTED_LOGIN_ENABLED": "true"},
    ],
)
def test_ephemeral_environment_requires_explicit_test_only_boundary(updates) -> None:
    environment = {
        "HOHU_RELEASE_QUALIFICATION_EPHEMERAL": "true",
        "ENV": "test",
        "TENANT_MODE": "single",
        "TENANT_HOSTED_LOGIN_ENABLED": "false",
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/ephemeral",
        "SECRET_KEY": "test-only-secret-key-with-32-characters",
        "REDIS_DB": "9",
    }
    environment.update(updates)
    if updates:
        with pytest.raises(ValueError):
            qualification.validate_ephemeral_environment(environment)
    else:
        qualification.validate_ephemeral_environment(environment)


def test_source_verification_requires_full_clean_head_and_external_output(
    monkeypatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "repository"
    project_root.mkdir()
    output = tmp_path / "artifacts" / "qualification.json"
    calls = iter(
        [
            MagicMock(stdout=f"{BUILD_SHA}\n"),
            MagicMock(stdout=""),
        ]
    )
    monkeypatch.setattr(
        qualification.subprocess,
        "run",
        lambda *_args, **_kwargs: next(calls),
    )

    assert (
        qualification.verify_source_and_output(
            expected_sha=BUILD_SHA,
            output=output,
            project_root=project_root,
        )
        == BUILD_SHA
    )

    with pytest.raises(ValueError, match="outside the checkout"):
        qualification.verify_source_and_output(
            expected_sha=BUILD_SHA,
            output=project_root / "qualification.json",
            project_root=project_root,
        )


def test_source_verification_rejects_dirty_or_abbreviated_revision(
    monkeypatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="full 40-character"):
        qualification.verify_source_and_output(
            expected_sha="abc123",
            output=tmp_path / "report.json",
            project_root=tmp_path / "repo",
        )

    project_root = tmp_path / "dirty-repository"
    project_root.mkdir()
    calls = iter(
        [MagicMock(stdout=f"{BUILD_SHA}\n"), MagicMock(stdout=" M app/main.py\n")]
    )
    monkeypatch.setattr(
        qualification.subprocess,
        "run",
        lambda *_args, **_kwargs: next(calls),
    )
    with pytest.raises(ValueError, match="clean Git checkout"):
        qualification.verify_source_and_output(
            expected_sha=BUILD_SHA,
            output=tmp_path / "outside" / "report.json",
            project_root=project_root,
        )


def test_source_verification_sanitizes_git_failures(
    monkeypatch, tmp_path: Path
) -> None:
    project_root = tmp_path / "repository"
    project_root.mkdir()
    monkeypatch.setattr(
        qualification.subprocess,
        "run",
        MagicMock(side_effect=OSError("sensitive host detail")),
    )

    with pytest.raises(ValueError, match="source verification failed") as exc_info:
        qualification.verify_source_and_output(
            expected_sha=BUILD_SHA,
            output=tmp_path / "outside" / "report.json",
            project_root=project_root,
        )

    assert "sensitive host detail" not in str(exc_info.value)


def test_runner_writes_sanitized_failure_report(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "artifacts" / "qualification.json"
    monkeypatch.setattr(
        qualification, "validate_ephemeral_environment", lambda _env: None
    )
    monkeypatch.setattr(
        qualification,
        "verify_source_and_output",
        lambda **_kwargs: BUILD_SHA,
    )
    monkeypatch.setattr(
        qualification,
        "_run_checked",
        MagicMock(side_effect=qualification.QualificationFailure("MIGRATION_FAILED")),
    )

    exit_code = qualification.run(
        Namespace(build_sha=BUILD_SHA, profile="ci", output=output, api_port=0)
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert report["qualified"] is False
    assert report["failureCodes"] == ["MIGRATION_FAILED"]
    assert report["secretsPersisted"] is False
    serialized = json.dumps(report).lower()
    assert "password" not in serialized
    assert "token" not in serialized


def test_runner_sanitizes_unexpected_failure_and_untrusted_build_sha(
    monkeypatch, tmp_path: Path
) -> None:
    output = tmp_path / "artifacts" / "qualification.json"
    monkeypatch.setattr(
        qualification, "validate_ephemeral_environment", lambda _env: None
    )
    monkeypatch.setattr(
        qualification,
        "verify_source_and_output",
        MagicMock(side_effect=RuntimeError("postgresql://user:secret@private/db")),
    )

    exit_code = qualification.run(
        Namespace(
            build_sha="not-a-revision-with-secret",
            profile="ci",
            output=output,
            api_port=0,
        )
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert report["buildSha"] == ""
    assert report["failureCodes"] == ["QUALIFICATION_UNEXPECTED_FAILURE"]
    assert "private" not in json.dumps(report)


def test_child_processes_force_utf8_for_cross_platform_seed_output(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PYTHONIOENCODING", "cp936")
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)

    environment = qualification._base_environment(BUILD_SHA, tmp_path)

    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["PYTHONUTF8"] == "1"
    assert environment["REDIS_PASSWORD"] == ""


def test_child_process_capture_decodes_forced_utf8(monkeypatch) -> None:
    run = MagicMock(
        return_value=qualification.subprocess.CompletedProcess(
            args=["child"], returncode=0, stdout="完成", stderr=""
        )
    )
    monkeypatch.setattr(qualification.subprocess, "run", run)

    qualification._run_checked(
        ["child"],
        environment={},
        failure_code="CHILD_FAILED",
    )

    assert run.call_args.kwargs["encoding"] == "utf-8"


def test_api_stops_before_temporary_evidence_cleanup(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[str] = []

    class TemporaryDirectory:
        def __enter__(self):
            return str(tmp_path)

        def __exit__(self, *_args):
            events.append("temporary-cleanup")

    monkeypatch.setattr(
        qualification.tempfile,
        "TemporaryDirectory",
        lambda **_kwargs: TemporaryDirectory(),
    )
    monkeypatch.setattr(
        qualification,
        "_stop_api",
        lambda _process, _log_handle: events.append("api-stop"),
    )

    with qualification._temporary_runtime() as (_temporary_root, runtime):
        runtime.process = MagicMock()
        runtime.log_handle = MagicMock()

    assert events == ["api-stop", "temporary-cleanup"]
    assert runtime.process is None
    assert runtime.log_handle is None


def test_windows_api_uses_process_group_and_graceful_break(
    monkeypatch, tmp_path: Path
) -> None:
    process = MagicMock()
    process.poll.return_value = None
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(qualification.os, "name", "nt")
    monkeypatch.setattr(
        qualification.subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        512,
        raising=False,
    )
    monkeypatch.setattr(
        qualification.signal,
        "CTRL_BREAK_EVENT",
        21,
        raising=False,
    )
    monkeypatch.setattr(qualification.subprocess, "Popen", popen)

    started, log_handle = qualification._start_api(
        environment={},
        port=9528,
        log_path=tmp_path / "api.log",
    )
    qualification._stop_api(started, log_handle)

    assert popen.call_args.kwargs["creationflags"] == 512
    process.send_signal.assert_called_once_with(21)
    process.terminate.assert_not_called()
    assert log_handle.closed is True


def test_final_snapshot_runs_only_after_api_shutdown(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[str] = []
    runtime = MagicMock()
    runtime.stop.side_effect = lambda: events.append("api-stop")
    monkeypatch.setattr(
        qualification,
        "_worker_json",
        lambda *_args, **_kwargs: events.append("cache-cleanup") or {},
    )
    monkeypatch.setattr(
        qualification,
        "_snapshot",
        lambda **_kwargs: events.append("snapshot") or {"redisDbSize": 0},
    )

    result = qualification._final_snapshot_after_shutdown(
        runtime=runtime,
        output=tmp_path / "final.json",
        target_tenant_id=22,
        control_tenant_id=33,
        environment={},
    )

    assert events == ["api-stop", "cache-cleanup", "snapshot"]
    assert result == {"redisDbSize": 0}


def test_workflows_gate_ci_and_release_image_on_qualification() -> None:
    root = Path(__file__).resolve().parents[2]
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (root / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "python -m tests.release.qualify_tenant_hosted_release" in ci
    assert "--profile ci" in ci
    assert "python -m tests.release.qualify_tenant_hosted_release" in release
    assert "--profile release" in release
    assert "needs: qualification" in release
    assert "actions/upload-artifact" in release
    assert "uv sync --locked --all-extras --dev" in ci
    assert "uv sync --locked --all-extras --dev" in release
    assert 'REDIS_PASSWORD: ""' in ci
    assert 'REDIS_PASSWORD: ""' in release
