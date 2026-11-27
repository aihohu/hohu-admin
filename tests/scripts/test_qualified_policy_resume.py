"""Prepared metadata permits resume only after verified completion and process exit."""

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy.documents import catalog_parts
from app.core.field_policy.maintenance import require_normal_resume
from app.core.field_policy.qualification import read_server_qualification
from app.core.process_identity import ProcessIdentity
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from tests.core.test_generic_activation_configuration import qualification_document
from tools.ops import authorization_maintenance as ops
from tools.ops import controlled_install as install
from tools.ops.policy_preparation_identity import PreparationSessions
from tools.ops.policy_prepare import PolicyPreparation


@pytest.fixture
def qualified_marker(tmp_path, monkeypatch):
    catalog = production_catalog()
    path = tmp_path / "qualification.json"
    path.write_text(json.dumps(qualification_document(catalog)), encoding="utf-8")
    monkeypatch.setattr(settings, "RELEASE_BUILD_SHA", "b" * 40)
    monkeypatch.setattr(settings, "FIELD_POLICY_QUALIFICATION_FILE", str(path))
    proof = read_server_qualification(catalog=catalog, required=True)
    action, adapter = catalog_parts(catalog)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identity = ProcessIdentity.read(process.pid)
        assert identity is not None
        completion = {
            "schema_version": 1,
            "build_sha": "b" * 40,
            "catalog_digest": catalog.digest,
            "action_digest": action,
            "adapter_digest": adapter,
            "qualification_digest": proof.evidence_digest,
            "expires_at": proof.expires_at.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "tenant_count": 2,
            "quarantined_count": 0,
            "state_digest": "c" * 64,
        }
        record = {
            "schema_version": 3,
            "state": "qualified-stopped",
            "build_sha": "b" * 40,
            "database_oid": 123,
            "manifest_digest": "d" * 64,
            "identity": {"pid": identity.pid, "birth": identity.birth},
            "catalog_digest": catalog.digest,
            "action_digest": action,
            "adapter_digest": adapter,
            "address": "127.0.0.1",
            "port": 8765,
            "business_background_tasks": [],
            "web_commit": None,
            "web_manifest_digest": None,
            "migration": {},
            "completion": completion,
        }
        guard = SimpleNamespace(
            directory=tmp_path,
            database_oid=123,
            manifest=SimpleNamespace(build_sha="b" * 40, digest="d" * 64),
        )
        yield guard, record, process, path
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "alive",
        "incomplete",
        "build",
        "oid",
        "manifest",
        "qualification",
        "digest",
        "unknown",
        "oversize",
    ],
)
def test_resume_validates_exact_completed_binding_and_terminated_preparer(
    qualified_marker, fault
):
    guard, record, process, qualification_path = qualified_marker
    if fault != "alive":
        process.terminate()
        process.wait(timeout=5)
    if fault == "incomplete":
        record["state"] = "preparing-stopped"
    if fault == "build":
        record["build_sha"] = "e" * 40
    if fault == "oid":
        record["database_oid"] += 1
    if fault == "manifest":
        record["manifest_digest"] = "e" * 64
    if fault == "qualification":
        qualification_path.write_text("{}", encoding="utf-8")
    if fault == "digest":
        record["completion"]["qualification_digest"] = "e" * 64
    if fault == "unknown":
        record["resume_anyway"] = True
    (guard.directory / "policy-prepare.json").write_text(
        "x" * 65537 if fault == "oversize" else json.dumps(record), encoding="utf-8"
    )
    if fault is None:
        require_normal_resume(guard)
    else:
        with pytest.raises(BusinessException) as denied:
            require_normal_resume(guard)
        assert denied.value.error_code == "FIELD_POLICY_ACTIVATION_BLOCKED"


@pytest.mark.parametrize("failure", [False, True])
async def test_explicit_finish_requires_acknowledged_complete_snapshot(
    qualified_marker, monkeypatch, failure
):
    guard, record, _, _ = qualified_marker
    guard.require_quiescent = AsyncMock()
    controller = object.__new__(PolicyPreparation)
    controller.guard, controller.processes, controller.bootstrap = guard, None, True
    controller.catalog = production_catalog()
    controller.lock, controller.finished = asyncio.Lock(), asyncio.Event()
    controller.identities = PreparationSessions()
    controller.completion = None
    identity = SimpleNamespace(tenant=object(), user_version=1)
    token = controller.identities.issue(identity)
    controller.runtime = SimpleNamespace(
        field_management=AsyncMock(),
        rollout_completion=AsyncMock(
            return_value=record["completion"],
            side_effect=RuntimeError("uncertain snapshot ACK") if failure else None,
        ),
    )
    controller._event = lambda *_args: None
    monkeypatch.setattr("tools.ops.policy_prepare._evidence", AsyncMock())
    if failure:
        with pytest.raises(RuntimeError):
            await controller.finish(token)
        assert controller.completion is None
    else:
        result = await controller.finish(token)
        assert result == {"state": "qualified-admission-closed"}
        assert controller.completion == record["completion"]
        controller.runtime.rollout_completion.assert_awaited_once()
    assert controller.finished.is_set()
    assert controller.identities.tokens == {}


async def test_removed_preparation_marker_cannot_bypass_alternate_resume(
    qualified_marker,
):
    guard, record, process, _ = qualified_marker
    process.terminate()
    process.wait(timeout=5)
    marker = guard.directory / "policy-prepare.json"
    marker.write_text(json.dumps(record), encoding="utf-8")
    stop = {
        "policy_prepare_required": True,
        "original_allow": True,
        "bootstrap": True,
        "observed_at": None,
    }
    (guard.directory / "stop.json").write_text(json.dumps(stop), encoding="utf-8")
    install.write_receipt(guard, True)
    marker.unlink()
    guard.require_quiescent = AsyncMock()
    guard.runtime_engine = SimpleNamespace(dispose=AsyncMock())
    guard._owned_backends = {}
    guard._allow = AsyncMock()
    with pytest.raises(BusinessException):
        await ops.PostgresStop._resume_admission(guard, stop)
    guard._allow.assert_not_awaited()


@pytest.mark.parametrize("fail_unlink", [False, True])
async def test_resume_archives_and_consumes_this_cycle_metadata(
    qualified_marker, monkeypatch, fail_unlink
):
    guard, record, process, _ = qualified_marker
    process.terminate()
    process.wait(timeout=5)
    (guard.directory / "policy-prepare.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    stop = {
        "policy_prepare_required": True,
        "original_allow": True,
        "bootstrap": True,
        "observed_at": None,
    }
    (guard.directory / "stop.json").write_text(json.dumps(stop), encoding="utf-8")
    install.write_receipt(guard, True)
    originals = {
        name: (guard.directory / name).read_bytes()
        for name in ("policy-prepare.json", "install.json", "stop.json")
    }
    guard.require_quiescent = AsyncMock()
    guard.runtime_engine = SimpleNamespace(dispose=AsyncMock())
    guard._owned_backends = {}
    guard._allow = AsyncMock()
    if fail_unlink:
        original_unlink = Path.unlink
        failures = []

        def fail_once(path, *args, **kwargs):
            if path == guard.directory / "install.json" and not failures:
                failures.append(True)
                raise PermissionError("exclusive test injection")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_once)
        with pytest.raises(PermissionError):
            await ops.PostgresStop._resume_admission(guard, stop)
        assert [item.args[0] for item in guard._allow.await_args_list] == [True, False]
        assert {
            name: (guard.directory / name).read_bytes() for name in originals
        } == originals
        guard._allow.reset_mock()
    await ops.PostgresStop._resume_admission(guard, stop)
    guard._allow.assert_awaited_once_with(True)
    for name, raw in originals.items():
        assert not (guard.directory / name).exists()
        saved = list((guard.directory / "completed").glob("*/" + name))
        assert len(saved) == 1 and saved[0].read_bytes() == raw
