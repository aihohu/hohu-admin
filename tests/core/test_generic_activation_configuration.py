"""Only bounded server configuration can qualify the production activation path."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy.documents import catalog_parts
from app.core.field_policy.qualification import CHECKS, read_server_qualification
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.schemas.field_management import parse_management


def qualification_document(catalog, build="b" * 40):
    action, adapter = catalog_parts(catalog)
    return {
        "schema_version": 2,
        "build_sha": build,
        "database_revision": "e020f3b00001",
        "catalog_digest": catalog.digest,
        "action_digest": action,
        "adapter_digest": adapter,
        "objects": sorted(catalog.objects),
        "checks": dict.fromkeys(CHECKS, True),
        "evidence": dict.fromkeys(CHECKS, "a" * 64),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }


@pytest.mark.parametrize(
    "fault", [None, "missing", "malformed", "expired", "old_build", "oversize"]
)
def test_server_qualification_never_opens_activation_on_invalid_file(
    tmp_path, monkeypatch, fault
):
    catalog = production_catalog()
    path = tmp_path / "server-qualification.json"
    doc = qualification_document(catalog)
    if fault == "expired":
        doc["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    if fault == "old_build":
        doc["build_sha"] = "c" * 40
    if fault != "missing":
        path.write_text(
            "{"
            if fault == "malformed"
            else "x" * (1024 * 1024 + 1)
            if fault == "oversize"
            else json.dumps(doc),
            encoding="utf-8",
        )
    monkeypatch.setattr(settings, "RELEASE_BUILD_SHA", "b" * 40)
    monkeypatch.setattr(settings, "FIELD_POLICY_QUALIFICATION_FILE", str(path))
    if fault is None:
        assert (
            read_server_qualification(catalog=catalog, required=True).catalog_digest
            == catalog.digest
        )
    else:
        assert read_server_qualification(catalog=catalog) is None
        with pytest.raises(BusinessException) as denied:
            read_server_qualification(catalog=catalog, required=True)
        assert denied.value.error_code == "FIELD_POLICY_ACTIVATION_BLOCKED"


def test_activation_dto_is_closed_and_preserves_string_versions():
    payload = {
        "requestId": "11111111-1111-1111-1111-111111111111",
        "reason": "reviewed candidate",
        "expectedVersion": "2",
        "expectedEpoch": "2",
        "catalogDigest": "a" * 64,
        "candidateManifestDigest": "b" * 64,
    }
    for operation in ("ready", "activate"):
        parsed = parse_management(operation, payload)
        assert parsed["expected_version"] == "2"
        for extra in (
            {"qualification": {}},
            {"tenantId": "1"},
            {"expectedVersion": 2},
            {"candidateManifestDigest": "invalid"},
        ):
            with pytest.raises(BusinessException):
                parse_management(operation, payload | extra)
