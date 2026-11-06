"""Closed, canonical documents for unactivated user policy candidates."""

import hashlib
import json
from dataclasses import asdict
from typing import Any

from app.core.exceptions import InvalidParameterException
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.user_fields import CATALOG_REVISION, USER_FIELDS

MAX_RULES = 256
MAX_DOCUMENT_BYTES = 65_536
_RULE_KEYS = frozenset(
    {"field_id", "operation", "purpose", "effect", "read", "mask_id"}
)


def _invalid() -> InvalidParameterException:
    return InvalidParameterException(
        "用户字段策略文档无效或超过限制", error_code="FIELD_POLICY_INVALID"
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def document_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def catalog_digest() -> str:
    fields = []
    for field_id in sorted(USER_FIELDS):
        item = asdict(USER_FIELDS[field_id])
        for key, value in item.items():
            if isinstance(value, frozenset):
                item[key] = sorted(value)
        fields.append(item)
    return document_digest({"revision": CATALOG_REVISION, "fields": fields})


def canonical_rules(
    grants: tuple[FieldGrant, ...], *, role_id: int
) -> list[dict[str, Any]]:
    if (
        isinstance(role_id, bool)
        or not isinstance(role_id, int)
        or not 0 < role_id <= 9_223_372_036_854_775_807
        or not isinstance(grants, tuple)
        or len(grants) > MAX_RULES
    ):
        raise _invalid()
    documents = []
    seen = set()
    for grant in grants:
        if not isinstance(grant, FieldGrant) or grant.role_id != str(role_id):
            raise _invalid()
        # Rebuild to reject malformed persisted/forged dataclasses as well.
        try:
            grant = FieldGrant(**asdict(grant))
            field = USER_FIELDS[grant.field_id]
        except (TypeError, ValueError, KeyError):
            raise _invalid() from None
        if grant.operation in {"read", "export"}:
            supported = field.readable and (
                field.read_purposes is None or grant.purpose in field.read_purposes
            )
        elif grant.operation in {"create", "update", "replace"}:
            supported = grant.operation in field.writable and (
                field.write_purposes is None or grant.purpose in field.write_purposes
            )
        else:
            supported = grant.operation in field.query_ops and (
                field.query_purposes is None or grant.purpose in field.query_purposes
            )
        key = (grant.field_id, grant.purpose, grant.operation)
        if not supported or key in seen:
            raise _invalid()
        seen.add(key)
        item = asdict(grant)
        del item["role_id"]
        documents.append(item)
    documents.sort(
        key=lambda item: (item["field_id"], item["purpose"], item["operation"])
    )
    if len(canonical_json(documents).encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise _invalid()
    return documents


def rules_digest(document: list[dict[str, Any]]) -> str:
    """Hash a document already checked by canonical_rules/read_rules."""
    return document_digest(document)


def read_rules(document: Any, *, role_id: int) -> tuple[FieldGrant, ...]:
    if not isinstance(document, list) or len(document) > MAX_RULES:
        raise _invalid()
    grants = []
    for item in document:
        if not isinstance(item, dict) or item.keys() != _RULE_KEYS:
            raise _invalid()
        if any(not isinstance(item[key], str) for key in _RULE_KEYS - {"mask_id"}):
            raise _invalid()
        if item["mask_id"] is not None and not isinstance(item["mask_id"], str):
            raise _invalid()
        try:
            grants.append(FieldGrant(role_id=str(role_id), **item))
        except (TypeError, ValueError, KeyError):
            raise _invalid() from None
    canonical = canonical_rules(tuple(grants), role_id=role_id)
    if canonical != document:
        raise _invalid()
    return tuple(grants)
