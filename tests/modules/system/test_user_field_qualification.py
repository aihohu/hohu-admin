"""Synthetic operator qualification records cannot be supplied by HTTP callers."""

import json

import pytest

from app.core.config import settings
from app.modules.system.field_policy import qualification as q
from app.modules.system.field_policy.documents import catalog_digest


@pytest.fixture
def qualification(tmp_path, monkeypatch):
    path = tmp_path / "qualification.json"
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_QUALIFICATION_FILE", str(path))
    monkeypatch.setattr(settings, "RELEASE_BUILD_SHA", "a" * 40)
    monkeypatch.setattr(settings, "ENV", "test")
    monkeypatch.setattr(q, "adapter_digest", lambda: "b" * 64)
    value = {
        "schema": 1,
        "build": "a" * 40,
        "catalogDigest": catalog_digest(),
        "adapterDigest": "b" * 64,
        "surfaces": dict.fromkeys(q.SURFACES, "passed"),
        "clients": {
            name: {"build": "a" * 40, "status": "passed"} for name in q.CLIENTS
        },
        "evidenceDigest": "c" * 64,
        "localCandidate": True,
    }

    def save():
        path.write_text(json.dumps(value), encoding="utf-8")

    return value, save


def test_qualification_requires_available_and_exact_build(qualification, monkeypatch):
    value, save = qualification
    assert q.qualification_blockers()
    save()
    assert not q.qualification_blockers()
    value["build"] = "d" * 40
    save()
    assert q.qualification_blockers()
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", False)
    assert q.qualification_blockers() == ("FIELD_POLICY_DEPLOYMENT_DISABLED",)


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("schema", True),
        ("adapterDigest", "a" * 64),
        ("surfaces", {}),
        ("clients", {}),
        ("evidenceDigest", ""),
        ("localCandidate", "false"),
    ],
)
def test_incomplete_or_mismatched_evidence_is_blocked(
    qualification, field, replacement
):
    value, save = qualification
    value[field] = replacement
    save()
    assert q.qualification_blockers()


def test_local_acceptance_is_not_production_qualification(qualification, monkeypatch):
    _, save = qualification
    save()
    monkeypatch.setattr(settings, "ENV", "prod")
    assert q.qualification_blockers()
