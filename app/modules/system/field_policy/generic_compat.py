"""Explicit user-fields/1 conversion, not a production dispatch registration.

Legacy documents are validated before expanding their implicit query uses.
The original document budget is enforced before the generic transport budget.
"""

from collections import defaultdict

from app.core.field_policy import Access, FieldRef, FieldSpec, Grant, ObjectSpec
from app.core.field_policy.contracts import identity, require, sequence, tenant
from app.modules.system.field_policy.documents import canonical_rules
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.user_fields import USER_FIELDS

OBJECT_ID = "system.user"
_PURPOSES = {
    "read": ("management.read", "own-account"),
    "export": ("export",),
    "create": ("management.create", "import"),
    "update": ("management.update", "import", "own-account"),
    "replace": ("management.relations", "import"),
    "exact": ("management.query", "import"),
    "contains": ("management.query",),
    "eq": ("management.query",),
    "group": ("management.query",),
    "distinct": ("management.query",),
}


def legacy_accesses(operation: str, purpose: str) -> tuple[Access, ...]:
    if operation in {"exact", "contains", "eq", "group", "distinct"}:
        if purpose == "import":
            return (Access(operation, purpose, "match"),)
        if operation == "eq":
            return (
                Access(operation, purpose, "filter"),
                Access(operation, purpose, "join"),
            )
        return (
            Access(
                operation,
                purpose,
                operation if operation in {"group", "distinct"} else "filter",
            ),
        )
    return (Access(operation, purpose),)


def user_compat_object() -> ObjectSpec:
    fields = []
    for field_id, field in USER_FIELDS.items():
        accesses = []
        for operation, purposes in _PURPOSES.items():
            if operation in {"read", "export"}:
                supported, allowed = field.readable, field.read_purposes
            elif operation in {"create", "update", "replace"}:
                supported, allowed = operation in field.writable, field.write_purposes
            else:
                supported, allowed = operation in field.query_ops, field.query_purposes
            if supported:
                for purpose in purposes:
                    if allowed is None or purpose in allowed:
                        accesses.extend(legacy_accesses(operation, purpose))
        kind = {
            "user_id": "id",
            "auth_version": "integer",
            "tenant_id": "integer",
            "email_verified": "boolean",
            "roles": "json",
            "departments": "json",
            "create_time": "datetime",
            "update_time": "datetime",
            "email_verified_at": "datetime",
        }.get(field_id, "string")
        fields.append(
            FieldSpec(
                field_id,
                kind,
                tuple(accesses),
                nullable=field_id != "user_id",
                mask_id=field.mask_id,
                structural=field_id == "user_id",
            )
        )
    # Up to two generic uses can represent one validated legacy query rule.
    return ObjectSpec(
        OBJECT_ID,
        "tenant",
        "user.scope",
        tuple(fields),
        max_rules=512,
        max_rule_bytes=262144,
    )


def convert_legacy_grants(
    tenant_id: int, grants: tuple[FieldGrant, ...]
) -> tuple[Grant, ...]:
    tenant(tenant_id)
    sequence(grants, 262144, FieldGrant)
    grouped = defaultdict(list)
    for grant in grants:
        identity(grant.role_id)
        grouped[grant.role_id].append(grant)
        require(len(grouped[grant.role_id]) <= 256)
    result = []
    for role_id, role_grants in grouped.items():
        canonical_rules(tuple(role_grants), role_id=int(role_id))
        for grant in role_grants:
            for access in legacy_accesses(grant.operation, grant.purpose):
                result.append(
                    Grant(
                        tenant_id,
                        role_id,
                        FieldRef(OBJECT_ID, grant.field_id),
                        access,
                        grant.effect,
                        grant.read,
                        grant.mask_id,
                    )
                )
    return tuple(result)
