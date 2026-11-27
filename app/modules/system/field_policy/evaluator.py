"""Pure decisions over trusted per-record role applicability.

The caller must authenticate, tenant/row-scope and function-authorize first.
No role IDs, purpose, entrypoint or grants may come from an untrusted request.
This module does not implement query-scope inclusion, own-account identity
checks, policy storage, activation, snapshots, caches or revocation barriers.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Any

from app.core.exceptions import AuthorizationException, InvalidParameterException
from app.modules.system.field_policy.user_fields import USER_FIELDS, UserField
from app.utils.mask_util import MaskUtil

_READ_OPERATIONS = frozenset({"read", "export"})
_WRITE_OPERATIONS = frozenset({"create", "update", "replace"})
_QUERY_OPERATIONS = frozenset({"exact", "contains", "eq", "group", "distinct"})
_PURPOSE_OPERATIONS = {
    "management.read": frozenset({"read"}),
    "management.create": frozenset({"create"}),
    "management.update": frozenset({"update"}),
    "management.relations": frozenset({"replace"}),
    "management.query": _QUERY_OPERATIONS,
    "export": frozenset({"export"}),
    "import": frozenset({"create", "update", "replace", "exact"}),
    "own-account": frozenset({"read", "update"}),
}
_ENTRYPOINTS = frozenset(
    {
        "user-list",
        "user-detail",
        "own-profile",
        "ai-tool",
        "export",
        "import",
        "relations",
        "password",
    }
)
_LEVELS = {"hidden": 0, "masked": 1, "plain": 2}


def _invalid() -> InvalidParameterException:
    # Never include values, raw field paths, grants or caller text in errors.
    return InvalidParameterException(
        "用户字段策略或输入上下文无效", error_code="FIELD_POLICY_INVALID"
    )


def _validate_operation_purpose(operation: str, purpose: str) -> None:
    if (
        purpose not in _PURPOSE_OPERATIONS
        or operation not in _PURPOSE_OPERATIONS[purpose]
    ):
        raise _invalid()


@dataclass(frozen=True)
class FieldGrant:
    role_id: str
    field_id: str
    operation: str
    purpose: str
    effect: str = "allow"
    read: str = "plain"
    mask_id: str | None = None

    def __post_init__(self) -> None:
        _validate_operation_purpose(self.operation, self.purpose)
        if not isinstance(self.role_id, str) or not self.role_id:
            raise _invalid()
        if self.field_id not in USER_FIELDS or self.effect not in {"allow", "deny"}:
            raise _invalid()
        field = USER_FIELDS[self.field_id]
        if self.read not in {"plain", "masked"}:
            raise _invalid()
        if self.read == "masked":
            if (
                self.operation not in _READ_OPERATIONS
                or self.mask_id is None
                or self.mask_id != field.mask_id
            ):
                raise _invalid()
        elif self.mask_id is not None:
            raise _invalid()


@dataclass(frozen=True)
class FieldDecision:
    allowed: bool = False
    read: str = "hidden"
    mask_id: str | None = None


@dataclass(frozen=True)
class FieldDecisionSet:
    operation: str
    purpose: str
    entrypoint: str
    fields: Mapping[str, FieldDecision]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


@dataclass(frozen=True)
class UserProjection:
    """Native scalar types and frozen containers; HTTP serialization is separate."""

    values: Mapping[str, Any]
    field_access: Mapping[str, FieldDecision]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(
            self, "field_access", MappingProxyType(dict(self.field_access))
        )


def _supports(field: UserField, operation: str, purpose: str) -> bool:
    if operation in _READ_OPERATIONS:
        return field.readable and (
            field.read_purposes is None or purpose in field.read_purposes
        )
    if operation in _WRITE_OPERATIONS:
        return operation in field.writable and (
            field.write_purposes is None or purpose in field.write_purposes
        )
    return operation in field.query_ops and (
        field.query_purposes is None or purpose in field.query_purposes
    )


def resolve_record_fields(
    *,
    catalog: Mapping[str, UserField],
    trusted_grants: tuple[FieldGrant, ...],
    applicable_role_ids: frozenset[str],
    operation: str,
    purpose: str,
    entrypoint: str,
) -> FieldDecisionSet:
    """Resolve one operation for one already authorized record.

    applicable_role_ids is the set of enabled actor roles whose *individual*
    row scope contains this record, not the actor's union of all role IDs.
    Confirmation/history must reevaluate the original purpose, never mint an
    independent grant. user_id is the structural anchor of an authorized row.
    Current management exports require export AND management.read grants over
    these same per-record inputs; callers cannot mix another record's decision.
    """
    _validate_operation_purpose(operation, purpose)
    if dict(catalog) != dict(USER_FIELDS) or entrypoint not in _ENTRYPOINTS:
        raise _invalid()
    if not isinstance(trusted_grants, tuple) or not all(
        isinstance(grant, FieldGrant) for grant in trusted_grants
    ):
        raise _invalid()
    if not isinstance(applicable_role_ids, frozenset) or not all(
        isinstance(role, str) and role for role in applicable_role_ids
    ):
        raise _invalid()
    applicable = tuple(
        grant
        for grant in trusted_grants
        if grant.role_id in applicable_role_ids
        and grant.operation == operation
        and grant.purpose == purpose
    )
    decisions = {}
    for field_id, field in catalog.items():
        if not _supports(field, operation, purpose):
            decisions[field_id] = FieldDecision()
            continue
        if field_id == "user_id" and operation in _READ_OPERATIONS:
            decisions[field_id] = FieldDecision(allowed=True, read="plain")
            continue
        rules = tuple(grant for grant in applicable if grant.field_id == field_id)
        if not rules or any(grant.effect == "deny" for grant in rules):
            decisions[field_id] = FieldDecision()
        elif operation in _READ_OPERATIONS:
            level = max((grant.read for grant in rules), key=_LEVELS.__getitem__)
            decisions[field_id] = FieldDecision(
                allowed=True,
                read=level,
                mask_id=field.mask_id if level == "masked" else None,
            )
        else:
            decisions[field_id] = FieldDecision(allowed=True)
    if operation == "export":
        source_read = resolve_record_fields(
            catalog=catalog,
            trusted_grants=trusted_grants,
            applicable_role_ids=applicable_role_ids,
            operation="read",
            purpose="management.read",
            entrypoint=entrypoint,
        )
        for field_id, decision in decisions.items():
            level = min(
                (decision.read, source_read.fields[field_id].read),
                key=_LEVELS.__getitem__,
            )
            decisions[field_id] = FieldDecision(
                allowed=level != "hidden",
                read=level,
                mask_id=catalog[field_id].mask_id if level == "masked" else None,
            )
    return FieldDecisionSet(operation, purpose, entrypoint, decisions)


def validate_write_fields(
    *, supplied_paths: Iterable[str], decisions: FieldDecisionSet, operation: str
) -> None:
    """Check all canonical supplied roots, including explicit null/unchanged values.

    Input adapters retain presence before defaults and validate aliases/nested
    relation values with their own schemas. Values/nullability/mask-placeholder
    rejection are separate from this authorization-only check. No partial result
    is returned and no caller input is modified.
    """
    if operation not in _WRITE_OPERATIONS or operation != decisions.operation:
        raise _invalid()
    for field_id in supplied_paths:
        decision = decisions.fields.get(field_id)
        if decision is None or not decision.allowed:
            raise AuthorizationException(
                "无权修改所提供的字段", error_code="FIELD_WRITE_DENIED"
            )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise _invalid()
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float, date, datetime)):
        return value
    # Never return an ORM object or arbitrary serializer with hidden attributes.
    raise _invalid()


def _mask(value: Any, mask_id: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid()
    if mask_id == "phone/v1":
        if len(value) == 11 and value.isascii() and value.isdigit():
            return MaskUtil.phone(value)
        return "***"
    if mask_id == "email/v1":
        local, separator, domain = value.rpartition("@")
        if separator and len(local) >= 2 and domain:
            return MaskUtil.email(value)
        return "***"
    raise _invalid()


def project_user_values(
    *,
    raw_values: Mapping[str, Any],
    decisions: FieldDecisionSet,
    entrypoint_caps: Mapping[str, str],
) -> UserProjection:
    """Project normalized DTO values through a closed, trusted entrypoint allowlist.

    Missing cap means no output and no capability metadata. Relation DTOs must
    already satisfy the referenced resource's own authorization and schemas.
    Never pass a raw ORM __dict__ or arbitrary nested client JSON.
    """
    if decisions.operation not in _READ_OPERATIONS or any(
        key not in USER_FIELDS for key in raw_values
    ):
        raise _invalid()
    values = {}
    access = {}
    for field_id, cap in entrypoint_caps.items():
        if field_id not in USER_FIELDS or cap not in _LEVELS:
            raise _invalid()
        field = USER_FIELDS[field_id]
        if cap == "masked" and field.mask_id is None:
            raise _invalid()
        decision = decisions.fields[field_id]
        # Current list/detail masking is an immutable upper bound, even if a
        # future adapter mistakenly supplies a plain capability cap.
        if (
            decisions.entrypoint in {"user-list", "user-detail"}
            and field.mask_id is not None
        ):
            cap = min((cap, "masked"), key=_LEVELS.__getitem__)
        level = min((decision.read, cap), key=_LEVELS.__getitem__)
        if not field.readable or not decision.allowed:
            level = "hidden"
        access[field_id] = FieldDecision(
            allowed=level != "hidden",
            read=level,
            mask_id=field.mask_id if level == "masked" else None,
        )
        if level == "hidden" or field_id not in raw_values:
            continue
        value = raw_values[field_id]
        if field_id == "user_id":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, str))
                or not str(value).isascii()
                or not str(value).isdigit()
            ):
                raise _invalid()
            value = str(value)
        elif level == "masked":
            value = _mask(value, field.mask_id)
        values[field_id] = _freeze(value)
    return UserProjection(values, access)
