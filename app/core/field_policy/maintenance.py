"""Persistent preparation completion checks; this never reopens admission."""

import re
from datetime import UTC, datetime

from app.core.exceptions import BusinessException
from app.core.field_policy.documents import catalog_parts, read_json
from app.core.field_policy.qualification import read_server_qualification
from app.core.process_identity import ProcessIdentity


def _blocked():
    return BusinessException(
        409,
        "通用字段策略尚未完成受控准备",
        error_code="FIELD_POLICY_ACTIVATION_BLOCKED",
    )


def require_normal_resume(guard, *, required=False):
    path = guard.directory / "policy-prepare.json"
    if not path.exists():
        if required:
            raise _blocked()
        return
    try:
        with path.open("rb") as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise _blocked()
        record = read_json(raw)
        keys = {
            "schema_version",
            "state",
            "build_sha",
            "database_oid",
            "manifest_digest",
            "identity",
            "catalog_digest",
            "action_digest",
            "adapter_digest",
            "address",
            "port",
            "business_background_tasks",
            "web_commit",
            "web_manifest_digest",
            "migration",
            "completion",
        }
        if (
            type(record) is not dict
            or set(record) != keys
            or type(record["schema_version"]) is not int
            or record["schema_version"] != 3
            or record["state"] != "qualified-stopped"
        ):
            raise _blocked()
        from app.modules.system.field_policy.ordinary_catalog import (  # noqa: PLC0415
            production_catalog,
        )

        catalog = production_catalog()
        proof = read_server_qualification(catalog=catalog, required=True)
        action, adapter = catalog_parts(catalog)
        common = {
            "build_sha": guard.manifest.build_sha,
            "catalog_digest": catalog.digest,
            "action_digest": action,
            "adapter_digest": adapter,
        }
        if (
            any(record[name] != value for name, value in common.items())
            or type(record["database_oid"]) is not int
            or record["database_oid"] != guard.database_oid
            or record["manifest_digest"] != guard.manifest.digest
            or record["address"] != "127.0.0.1"
            or type(record["port"]) is not int
            or not 1024 <= record["port"] <= 65535
            or record["business_background_tasks"] != []
            or type(record["migration"]) is not dict
        ):
            raise _blocked()
        proof.verify(catalog, guard.manifest.build_sha)
        completion = record["completion"]
        if type(completion) is not dict or set(completion) != set(common) | {
            "schema_version",
            "qualification_digest",
            "expires_at",
            "completed_at",
            "tenant_count",
            "quarantined_count",
            "state_digest",
        }:
            raise _blocked()
        if (
            any(completion[name] != value for name, value in common.items())
            or type(completion["schema_version"]) is not int
            or completion["schema_version"] != 1
            or completion["qualification_digest"] != proof.evidence_digest
            or completion["expires_at"] != proof.expires_at.isoformat()
            or type(completion["tenant_count"]) is not int
            or not 1 <= completion["tenant_count"] <= 1024
            or type(completion["quarantined_count"]) is not int
            or not 0 <= completion["quarantined_count"] <= completion["tenant_count"]
            or type(completion["state_digest"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", completion["state_digest"]) is None
            or type(completion["completed_at"]) is not str
        ):
            raise _blocked()
        completed = datetime.fromisoformat(completion["completed_at"])
        if (
            completed.tzinfo is None
            or completed > datetime.now(UTC)
            or completed >= proof.expires_at
        ):
            raise _blocked()
        identity = record["identity"]
        if (
            type(identity) is not dict
            or set(identity) != {"pid", "birth"}
            or type(identity["pid"]) is not int
            or not 0 < identity["pid"] < 2**31
            or type(identity["birth"]) is not str
            or not 1 <= len(identity["birth"]) <= 128
        ):
            raise _blocked()
        # A still-live or reused PID is unknown, never evidence of termination.
        if ProcessIdentity.read(identity["pid"]) is not None:
            raise _blocked()
    except (OSError, ValueError, TypeError, KeyError, BusinessException):
        raise _blocked() from None
