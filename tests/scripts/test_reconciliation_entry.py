"""Existing reconciliation CLI delegates writes only to the stop controller."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.authorization_maintenance import StoppedWritersEvidence
from app.core.exceptions import BusinessException
from scripts import reconcile_ai_legacy as script
from tools.ops import authorization_maintenance as ops


async def test_annotation_cli_uses_actual_stop_controller(tmp_path, monkeypatch):
    manifest = tmp_path / "inventory.json"
    manifest.write_text(
        json.dumps([{"logId": "1", "tenantId": "0", "baseline": "a" * 64}])
    )
    output = tmp_path / "result.json"
    args = SimpleNamespace(
        command="annotate",
        tenant_id=0,
        operator_id=1,
        manifest=manifest,
        reason="LEGACY_RUNNING",
        evidence_sha256="a" * 64,
        maintenance_sha256="b" * 64,
        inventory=tmp_path / "deployment.json",
        control_dir=tmp_path / "control",
        output=output,
    )
    dispatch = AsyncMock(return_value={"result": [{"disposition": "unverified"}]})
    monkeypatch.setattr(ops, "run_command", dispatch)
    await script.run(args)
    dispatch.assert_awaited_once()
    command = dispatch.call_args.args[0]
    assert (command.command, command.action, command.inventory) == (
        "execute",
        "ai.reconcile",
        args.inventory,
    )
    assert json.loads(output.read_text()) == [{"disposition": "unverified"}]


@pytest.mark.parametrize("expected", [None, "c" * 64, "d" * 64])
async def test_controller_binds_runtime_stop_digest(monkeypatch, expected):
    evidence = StoppedWritersEvidence(
        "unit",
        "0.2.0",
        "a" * 40,
        "c" * 64,
        datetime.now(UTC),
        frozenset({"api", "worker", "scheduler", "web-write-ingress"}),
        0,
        True,
    )
    guard = SimpleNamespace(
        manifest=SimpleNamespace(build_sha="a" * 40),
        runtime_engine=object(),
        observe=AsyncMock(return_value=evidence),
    )
    annotate = AsyncMock(return_value=[])
    monkeypatch.setattr(
        ops,
        "AuthorizationRuntime",
        lambda _: SimpleNamespace(reconciliation=SimpleNamespace(annotate=annotate)),
    )
    payload = {"maintenance_sha256": expected}
    if expected == "d" * 64:
        with pytest.raises(BusinessException):
            await ops.execute_action(guard, object(), "ai.reconcile", payload)
        annotate.assert_not_awaited()
    else:
        await ops.execute_action(guard, object(), "ai.reconcile", payload)
        assert (
            annotate.call_args.kwargs["maintenance_sha256"] == evidence.manifest_digest
        )
    assert payload["maintenance_sha256"] == expected
