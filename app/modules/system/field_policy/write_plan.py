"""Canonical write footprints retain presence independently of value equality."""

from types import MappingProxyType

from app.core.exceptions import InvalidParameterException
from app.modules.system.field_policy.evaluator import (
    FieldGrant,
    resolve_record_fields,
    validate_write_fields,
)
from app.modules.system.field_policy.user_fields import USER_FIELDS
from app.modules.system.schemas.user import (
    UpdateProfile,
    UserCreate,
    UserPatch,
    UserToolUpdate,
    UserUpdate,
)


def user_write_footprint(payload):
    if type(payload) not in {
        UserCreate,
        UserPatch,
        UserUpdate,
        UserToolUpdate,
        UpdateProfile,
    }:
        raise InvalidParameterException(
            "用户字段写计划无效", error_code="FIELD_POLICY_INVALID"
        )
    fields = frozenset(payload.model_fields_set)
    operation = "create" if type(payload) is UserCreate else "update"
    purpose = (
        "own-account" if type(payload) is UpdateProfile else f"management.{operation}"
    )
    relations = {"role_ids": "roles", "dept_ids": "departments"}
    groups = {(operation, purpose): fields - relations.keys()}
    if fields & relations.keys():
        groups[("replace", "management.relations")] = frozenset(
            relations[name] for name in fields & relations.keys()
        )
    return MappingProxyType(groups)


def validate_user_payload(payload, *, resolve):
    for (operation, purpose), fields in user_write_footprint(payload).items():
        decisions = resolve(operation, purpose)
        if decisions.operation != operation or decisions.purpose != purpose:
            raise InvalidParameterException(
                "字段写决策上下文不匹配", error_code="FIELD_POLICY_INVALID"
            )
        validate_write_fields(
            supplied_paths=fields, decisions=decisions, operation=operation
        )


def validate_legacy_user_payload(payload):
    """Compatibility only, after the transaction rejected non-legacy/ready state.

    Function, record and relationship authorities are still mandatory. These
    grants are code-owned legacy behavior, never an active policy or a caller
    flag that enables enforcement. Future activation must replace this resolver
    with current per-record policy decisions at this same validation boundary.
    """

    def resolve(operation, purpose):
        return resolve_record_fields(
            catalog=USER_FIELDS,
            trusted_grants=tuple(
                FieldGrant("legacy", name, operation, purpose) for name in USER_FIELDS
            ),
            applicable_role_ids=frozenset({"legacy"}),
            operation=operation,
            purpose=purpose,
            entrypoint="own-profile" if purpose == "own-account" else "user-detail",
        )

    validate_user_payload(payload, resolve=resolve)
