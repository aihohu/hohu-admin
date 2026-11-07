"""Qualification must cover the exact build/catalog and every required evidence."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import BusinessException
from app.core.field_policy import Catalog
from app.core.field_policy.documents import catalog_parts
from app.core.field_policy.qualification import CHECKS, read_qualification
from app.modules.system.field_policy.generic_compat import user_compat_object


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "build",
        "schema",
        "object",
        "check",
        "evidence",
        "expired",
        "unknown",
        "malformed",
    ],
)
def test_exact_qualification_contract(tmp_path, fault):
    catalog = Catalog((user_compat_object(),), ())
    action, adapter = catalog_parts(catalog)
    doc = {
        "schema_version": 2,
        "build_sha": "b" * 40,
        "database_revision": "e020f1200001",
        "catalog_digest": catalog.digest,
        "action_digest": action,
        "adapter_digest": adapter,
        "objects": sorted(catalog.objects),
        "checks": dict.fromkeys(CHECKS, True),
        "evidence": dict.fromkeys(CHECKS, "a" * 64),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    if fault == "build":
        doc["build_sha"] = "c" * 40
    if fault == "schema":
        doc["schema_version"] = True
    if fault == "object":
        doc["objects"] = []
    if fault == "check":
        doc["checks"]["direct_stop"] = False
    if fault == "evidence":
        doc["evidence"].pop("all_writers")
    if fault == "expired":
        doc["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    if fault == "unknown":
        doc["trusted"] = True
    if fault == "malformed":
        doc["expires_at"] = "bad"
    path = tmp_path / "qualification.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    if fault is None:
        assert (
            read_qualification(path, catalog=catalog, build_sha="b" * 40).catalog_digest
            == catalog.digest
        )
    else:
        with pytest.raises(BusinessException):
            read_qualification(path, catalog=catalog, build_sha="b" * 40)
