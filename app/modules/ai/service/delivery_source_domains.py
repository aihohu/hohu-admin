"""Bounded, value-free grouping of the actual source records and field uses."""

import hashlib
import json
import re

from app.core.exceptions import BusinessException
from app.core.field_policy import Access, FieldRef, FieldUse
from app.core.field_policy.contracts import LEVELS, canonical, identity, limit, require
from app.modules.system.field_policy.delivery_catalog import READ, delivery_catalog

DOMAIN_KEYS = frozenset({"action_id", "object_id", "records", "read", "query"})
RECEIPT_KEYS = frozenset(
    {
        "schema",
        "adapter",
        "authority",
        "catalog_digest",
        "source_complete",
        "source_domains",
        "digest",
    }
)
MAX_BYTES = 4 * 1024 * 1024
RECORD_LIMITS = {
    "system.user": 5000,
    "system.dept": 5000,
    "system.role": 1024,
    "system.menu": 8192,
    "ai.agent": 1024,
}


def _hash(value):
    return hashlib.sha256(canonical(value, MAX_BYTES)).hexdigest()


def source_domain(*, action_id, object_id, records, read=(), query=()):
    require(type(records) is tuple and type(read) is tuple and type(query) is tuple)
    limit(
        len(records) <= RECORD_LIMITS.get(object_id, 5000)
        and len(read) <= 256
        and len(query) <= 256
    )
    require(all(type(value) is int and value > 0 for value in records))
    require(
        all(
            type(pair) is tuple
            and len(pair) == 2
            and all(type(part) is str for part in pair)
            for pair in read
        )
    )
    require(
        all(
            type(parts) is tuple
            and len(parts) == 4
            and all(type(part) is str for part in parts)
            for parts in query
        )
    )
    value = {
        "action_id": action_id,
        "object_id": object_id,
        "records": sorted({str(value) for value in records}, key=int),
        "read": sorted([list(value) for value in set(read)]),
        "query": sorted([list(value) for value in set(query)]),
    }
    validate_domain(value)
    return value


def validate_domain(value):
    require(type(value) is dict)
    if value.get("kind") == "file":
        require(
            set(value)
            == {"kind", "file_id", "action_id", "read", "sha256", "manifest_digest"}
        )
        identity(value["file_id"])
        require(value["action_id"] in {"file.parse", "file.chat-read"})
        require(
            type(value["read"]) is list
            and "content" in value["read"]
            and value["read"] == sorted(set(value["read"]))
            and set(value["read"])
            <= {"content", "original_name", "file_size", "mime_type"}
        )
        require(
            all(
                type(value[k]) is str and re.fullmatch(r"[0-9a-f]{64}", value[k])
                for k in ("sha256", "manifest_digest")
            )
        )
        return value
    if value.get("kind") == "artifact":
        require(
            set(value)
            == {
                "kind",
                "resource_kind",
                "resource_id",
                "source_kind",
                "lineage_digest",
                "read",
            }
        )
        require(
            value["resource_kind"] in {"export", "import"}
            and value["source_kind"]
            in (
                {"export"}
                if value["resource_kind"] == "export"
                else {"preview_source", "import_errors"}
            )
        )
        require(
            type(value["resource_id"]) is str
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value["resource_id"])
        )
        require(
            type(value["lineage_digest"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", value["lineage_digest"])
        )
        require(
            type(value["read"]) is list
            and value["read"] == sorted(set(value["read"]))
            and all(
                type(n) is str
                and FieldRef("transfer." + value["resource_kind"], n)
                in delivery_catalog().fields
                for n in value["read"]
            )
        )
        return value
    if value.get("kind") == "input":
        require(
            set(value) == {"kind", "read"}
            and type(value["read"]) is list
            and value["read"] in [["content"], ["content", "parts"]]
        )
        return value
    if value.get("kind") == "completion":
        require(set(value) == {"kind", "log_id", "tool_call_id", "status"})
        identity(value["log_id"])
        require(
            type(value["tool_call_id"]) is str
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value["tool_call_id"]) is not None
        )
        require(
            value["status"] in {"success", "failed", "rejected", "expired", "unknown"}
        )
        return value
    require(set(value) == DOMAIN_KEYS)
    catalog = delivery_catalog()
    require(type(value["action_id"]) is str and value["action_id"] in catalog.actions)
    action = catalog.actions[value["action_id"]]
    require(type(value["object_id"]) is str and value["object_id"] in action.object_ids)
    records, reads, queries = value["records"], value["read"], value["query"]
    require(type(records) is list and type(reads) is list and type(queries) is list)
    limit(
        len(records) <= RECORD_LIMITS.get(value["object_id"], 5000)
        and len(reads) <= 256
        and len(queries) <= 256
    )
    for identifier in records:
        identity(identifier)
    require(records == sorted(set(records), key=int))
    for pair in reads:
        require(
            type(pair) is list
            and len(pair) == 2
            and all(type(part) is str for part in pair)
        )
        ref, level = FieldRef(value["object_id"], pair[0]), pair[1]
        require(
            ref in catalog.fields
            and FieldUse(ref, READ) in action.uses
            and level in {"plain", "masked"}
        )
        spec = catalog.fields[ref]
        require(
            LEVELS[level] <= LEVELS[spec.read_cap]
            and (level != "masked" or spec.mask_id is not None)
        )
    for parts in queries:
        require(
            type(parts) is list
            and len(parts) == 4
            and all(type(part) is str for part in parts)
        )
        ref = FieldRef(value["object_id"], parts[0])
        access = Access(*parts[1:])
        require(
            (
                access.purpose == "management.query"
                or access == Access("exact", "import", "match")
            )
            and FieldUse(ref, access) in action.uses
        )
    require(reads == [list(v) for v in sorted({tuple(v) for v in reads})])
    require(queries == [list(v) for v in sorted({tuple(v) for v in queries})])
    return value


def normalize_domains(domains):
    require(type(domains) in {list, tuple})
    limit(len(domains) <= 256)
    grouped = {}
    for value in domains:
        validate_domain(value)
        body = {key: val for key, val in value.items() if key != "records"}
        key = json.dumps(body, sort_keys=True, separators=(",", ":"))
        if key not in grouped:
            grouped[key] = json.loads(json.dumps(value))
        elif "records" in value:
            grouped[key]["records"] = sorted(
                set(grouped[key]["records"]) | set(value["records"]), key=int
            )
            limit(
                len(grouped[key]["records"])
                <= RECORD_LIMITS.get(value["object_id"], 5000)
            )
    result = [grouped[key] for key in sorted(grouped)]
    limit(len(result) <= 64)
    limit(sum(len(v.get("records", ())) for v in result) <= 16384)
    work = sum(
        len(v.get("records", ())) * (len(v.get("read", ())) + len(v.get("query", ())))
        for v in result
    )
    limit(work <= 262144)
    canonical(result, MAX_BYTES)
    return result


def validate_receipt(value):
    from app.modules.ai.service.delivery_authority import (  # noqa: PLC0415
        validate_delivery_authority,  # noqa: PLC0415
    )
    from app.modules.ai.service.field_authority import (  # noqa: PLC0415
        validate_field_authority,  # noqa: PLC0415
    )

    try:
        if type(value) is not dict or set(value) != RECEIPT_KEYS:
            return None
        if (
            type(value["schema"]) is not int
            or value["schema"] != 3
            or value["adapter"] != "ai-sources/v3"
            or value["catalog_digest"] != delivery_catalog().digest
            or value["source_complete"] is not True
        ):
            return None
        authority = value["authority"]
        if (
            type(authority) is not dict
            or authority.get("schema") not in {1, 2}
            or (
                validate_field_authority(authority) is None
                and validate_delivery_authority(authority) is None
            )
        ):
            return None
        if (
            not value["source_domains"]
            or normalize_domains(value["source_domains"]) != value["source_domains"]
        ):
            return None
        body = {k: v for k, v in value.items() if k != "digest"}
        if value["digest"] != _hash(body):
            return None
    except (BusinessException, TypeError, ValueError, OverflowError, RecursionError):
        return None
    return value


def with_sources(authority, domains):
    from app.modules.ai.service.delivery_authority import (  # noqa: PLC0415
        validate_delivery_authority,  # noqa: PLC0415
    )
    from app.modules.ai.service.field_authority import (  # noqa: PLC0415
        validate_field_authority,  # noqa: PLC0415
    )

    if type(authority) is dict and authority.get("schema") == 3:
        require(validate_receipt(authority) is not None)
        domains = (*authority["source_domains"], *domains)
        authority = authority["authority"]
    require(
        validate_field_authority(authority) is not None
        or validate_delivery_authority(authority) is not None
    )
    groups = normalize_domains(domains)
    require(bool(groups))
    value = {
        "schema": 3,
        "adapter": "ai-sources/v3",
        "authority": dict(authority),
        "catalog_digest": delivery_catalog().digest,
        "source_complete": True,
        "source_domains": groups,
    }
    value["digest"] = _hash(value)
    require(validate_receipt(value) is not None)
    return value


def authority_header(value):
    require(type(value) is dict)
    if value.get("schema") == 3:
        require(validate_receipt(value) is not None)
        return value["authority"]
    return value
