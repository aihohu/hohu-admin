"""Historical strings are never sufficient evidence of a user's identity."""

import pytest

from app.core.exceptions import BusinessException
from app.modules.system.field_policy.evaluator import FieldGrant, resolve_record_fields
from app.modules.system.field_policy.lineage import (
    UserFieldSource,
    project_identity_copy,
)
from app.modules.system.field_policy.user_fields import USER_FIELDS


def decision(*rules):
    return resolve_record_fields(
        catalog=USER_FIELDS,
        trusted_grants=rules,
        applicable_role_ids=frozenset({"1"}),
        operation="read",
        purpose="management.read",
        entrypoint="user-list",
    )


def test_missing_or_foreign_source_is_hidden_without_guessing_by_name():
    allowed = decision(FieldGrant("1", "user_name", "read", "management.read"))
    for source in (
        None,
        UserFieldSource(1, 100, "user_name", "management.read"),
        UserFieldSource(0, 101, "user_name", "management.read"),
    ):
        result = project_identity_copy(
            source=source,
            current_identity=(0, 100),
            current_values={"user_name": "Alice"},
            decisions=allowed,
        )
        assert result.visible is False and result.value is None


def test_current_field_decision_controls_the_copy_and_preserves_real_null():
    source = UserFieldSource(0, 100, "nickname", "management.read")
    hidden = project_identity_copy(
        source=source,
        current_identity=(0, 100),
        current_values={"nickname": "private"},
        decisions=decision(),
    )
    assert not hidden.visible
    visible = project_identity_copy(
        source=source,
        current_identity=(0, 100),
        current_values={"nickname": None},
        decisions=decision(FieldGrant("1", "nickname", "read", "management.read")),
    )
    assert visible.visible and visible.value is None


def test_copy_cannot_change_purpose_or_bypass_mask_cap():
    masked = project_identity_copy(
        source=UserFieldSource(0, 100, "user_phone", "management.read"),
        current_identity=(0, 100),
        current_values={"user_phone": "13812341234"},
        decisions=decision(FieldGrant("1", "user_phone", "read", "management.read")),
    )
    assert masked.visible and masked.value == "138****1234"
    wrong = project_identity_copy(
        source=UserFieldSource(0, 100, "user_phone", "own-account"),
        current_identity=(0, 100),
        current_values={"user_phone": "13812341234"},
        decisions=decision(FieldGrant("1", "user_phone", "read", "management.read")),
    )
    assert not wrong.visible


@pytest.mark.parametrize("field", ["hashed_password", "password", "roles", "tenant_id"])
def test_secret_or_structured_fields_cannot_be_claimed_as_identity_copies(field):
    with pytest.raises(BusinessException):
        UserFieldSource(0, 100, field, "management.read")
