"""Value-free all-object receipts. A digest detects mutation, never grants access."""

import hashlib
import json
import re

from app.core.exceptions import AuthorizationException
from app.modules.system.field_policy.delivery_catalog import delivery_catalog

KEYS = frozenset(
    {
        "schema",
        "adapter",
        "tenant_id",
        "actor_id",
        "global_epoch",
        "tenant_epoch",
        "active_digest",
        "catalog_digest",
        "build",
        "digest",
    }
)


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def _denied():
    return AuthorizationException(
        "字段授权来源已变化", error_code="FIELD_AUTHORIZATION_CHANGED"
    )


def validate_delivery_authority(value):
    if type(value) is dict and value.get("schema") == 3:
        from app.modules.ai.service.delivery_source_domains import (  # noqa: PLC0415
            validate_receipt,  # noqa: PLC0415
        )

        return validate_receipt(value)
    if type(value) is not dict or set(value) != KEYS:
        return None
    try:
        if (
            type(value["schema"]) is not int
            or value["schema"] != 2
            or value["adapter"] != "ai-delivery/v2"
            or value["catalog_digest"] != delivery_catalog().digest
            or type(value["build"]) is not str
            or not 1 <= len(value["build"]) <= 1024
            or type(value["active_digest"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["active_digest"]) is None
        ):
            return None
        for key in ("tenant_id", "actor_id"):
            part = value[key]
            if (
                type(part) is not str
                or re.fullmatch(r"0|[1-9][0-9]{0,18}", part) is None
                or not (0 if key == "tenant_id" else 1) <= int(part) < 2**63
            ):
                return None
        if any(
            type(value[k]) is not int or not 0 <= value[k] < 2**63
            for k in ("global_epoch", "tenant_epoch")
        ):
            return None
        if len(json.dumps(value).encode()) > 65536 or value["digest"] != _digest(
            {k: v for k, v in value.items() if k != "digest"}
        ):
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return value


def freeze_delivery_authority(
    *, tenant_id, actor_id, global_epoch, tenant_epoch, active_digest, build
):
    if type(tenant_id) is not int or type(actor_id) is not int:
        raise _denied()
    value = {
        "schema": 2,
        "adapter": "ai-delivery/v2",
        "tenant_id": str(tenant_id),
        "actor_id": str(actor_id),
        "global_epoch": global_epoch,
        "tenant_epoch": tenant_epoch,
        "active_digest": active_digest,
        "catalog_digest": delivery_catalog().digest,
        "build": build,
    }
    value["digest"] = _digest(value)
    if validate_delivery_authority(value) is None:
        raise _denied()
    return value


def require_delivery_authority(frozen, current):
    from app.modules.ai.service.delivery_source_domains import (  # noqa: PLC0415
        authority_header,  # noqa: PLC0415
    )

    if type(frozen) is dict and frozen.get("schema") == 3:
        frozen = authority_header(frozen)
    if type(current) is dict and current.get("schema") == 3:
        current = authority_header(current)
    if (
        validate_delivery_authority(frozen) is None
        or validate_delivery_authority(current) is None
        or frozen != current
    ):
        raise _denied()
