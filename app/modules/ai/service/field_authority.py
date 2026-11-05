"""Closed field-dependency evidence; contains identifiers, never business values.

The hash detects accidental alteration; it is not a signature or a grant. Only
an authorization transaction may mint evidence from database-owned authority.
"""

import hashlib
import json
import re

from app.core.exceptions import AuthorizationException
from app.modules.system.field_policy.documents import catalog_digest
from app.modules.system.field_policy.user_fields import CATALOG_REVISION, USER_FIELDS

PURPOSES = frozenset({"ai.run", "ai.confirmation", "ai.result", "ai.query"})
KEYS = frozenset(
    {
        "schema",
        "adapter",
        "tenant_id",
        "actor_id",
        "object",
        "purpose",
        "global_epoch",
        "tenant_epoch",
        "mode",
        "active_digest",
        "build",
        "catalog_revision",
        "catalog_digest",
        "fields",
        "digest",
    }
)


def denied(code="FIELD_AUTHORIZATION_CHANGED"):
    return AuthorizationException("字段授权依赖已失效，请重新发起操作", error_code=code)


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def freeze_field_authority(
    *,
    tenant_id,
    actor_id,
    global_epoch,
    tenant_epoch,
    mode,
    active_digest,
    build,
    purpose,
    fields,
):
    if (
        type(tenant_id) is not int
        or not 0 <= tenant_id < 2**63
        or type(actor_id) is not int
        or not 0 < actor_id < 2**63
        or type(fields) is not tuple
        or len(fields) > 256
        or any(type(item) is not str for item in fields)
    ):
        raise denied()
    value = {
        "schema": 1,
        "adapter": "ai-field-v1",
        "tenant_id": str(tenant_id),
        "actor_id": str(actor_id),
        "object": "system.user",
        "purpose": purpose,
        "global_epoch": global_epoch,
        "tenant_epoch": tenant_epoch,
        "mode": mode,
        "active_digest": active_digest,
        "build": build,
        "catalog_revision": CATALOG_REVISION,
        "catalog_digest": catalog_digest(),
        "fields": sorted(set(fields)),
    }
    value["digest"] = _digest(value)
    if validate_field_authority(value) is None:
        raise denied()
    return value


def validate_field_authority(value):
    if type(value) is not dict or set(value) != KEYS:
        return None
    try:
        if (
            type(value["schema"]) is not int
            or value["schema"] != 1
            or value["adapter"] != "ai-field-v1"
            or value["object"] != "system.user"
            or value["purpose"] not in PURPOSES
            or value["mode"] not in {"legacy", "ready", "enforcing", "frozen"}
            or type(value["build"]) is not str
            or not 1 <= len(value["build"]) <= 1024
            or value["catalog_revision"] != CATALOG_REVISION
            or value["catalog_digest"] != catalog_digest()
            or type(value["fields"]) is not list
            or len(value["fields"]) > 256
            or any(
                type(item) is not str
                or item not in USER_FIELDS
                or not USER_FIELDS[item].readable
                for item in value["fields"]
            )
            or value["fields"] != sorted(set(value["fields"]))
        ):
            return None
        for name in ("tenant_id", "actor_id"):
            item = value[name]
            if (
                type(item) is not str
                or not re.fullmatch(r"0|[1-9][0-9]{0,18}", item)
                or not (0 if name == "tenant_id" else 1) <= int(item) < 2**63
            ):
                return None
        for name in ("global_epoch", "tenant_epoch"):
            if type(value[name]) is not int or not 0 <= value[name] < 2**63:
                return None
        active = value["active_digest"]
        if active is not None and (
            type(active) is not str or not re.fullmatch(r"[0-9a-f]{64}", active)
        ):
            return None
        if value["mode"] in {"legacy", "ready"} and active is not None:
            return None
        if value["mode"] == "enforcing" and active is None:
            return None
        body = {key: item for key, item in value.items() if key != "digest"}
        if len(json.dumps(value).encode()) > 65536 or value["digest"] != _digest(body):
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return value


def require_current_field_authority(frozen, current, *, confirmation=False):
    if validate_field_authority(frozen) is None:
        raise denied(
            "FIELD_CONFIRMATION_LINEAGE_REQUIRED"
            if confirmation
            else "FIELD_AUTHORIZATION_CHANGED"
        )
    if validate_field_authority(current) is None or frozen != current:
        raise denied()
