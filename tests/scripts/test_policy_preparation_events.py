"""Preparation security events contain only server facts and have a fixed budget."""

import json
from types import SimpleNamespace

import pytest

from tools.ops.policy_preparation_events import record_event


def test_event_records_verified_identity_and_no_payload(tmp_path):
    identity = SimpleNamespace(tenant=SimpleNamespace(tenant_id=0, actor_user_id=101))
    record_event(tmp_path, "login", "accepted", identity)
    record_event(tmp_path, "login", "INVALID_CREDENTIALS", None)
    records = [
        json.loads(line)
        for line in (tmp_path / "policy-events.jsonl").read_text().splitlines()
    ]
    assert set(records[0]) == {"at", "operation", "code", "tenantId", "actorId"}
    assert records[0]["actorId"] == "101"
    assert records[1]["actorId"] is None


def test_full_event_log_cannot_be_silently_overwritten(tmp_path):
    path = tmp_path / "policy-events.jsonl"
    path.write_bytes(b"x" * (8 * 1024 * 1024))
    with pytest.raises(ValueError):
        record_event(tmp_path, "login", "INVALID_CREDENTIALS", None)
    assert path.stat().st_size == 8 * 1024 * 1024
