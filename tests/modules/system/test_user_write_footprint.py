"""Every carried write field is checked, including unchanged values and null."""

import pytest

from app.core.exceptions import BusinessException
from app.modules.system.field_policy.evaluator import FieldGrant, resolve_record_fields
from app.modules.system.field_policy.user_fields import USER_FIELDS
from app.modules.system.field_policy.write_plan import (
    user_write_footprint,
    validate_user_payload,
)
from app.modules.system.schemas.user import UserCreate, UserPatch, UserUpdate


def resolver(operation, purpose):
    return resolve_record_fields(
        catalog=USER_FIELDS,
        trusted_grants=(FieldGrant("1", "nickname", operation, purpose),),
        applicable_role_ids=frozenset({"1"}),
        operation=operation,
        purpose=purpose,
        entrypoint="user-detail",
    )


@pytest.mark.parametrize(
    "payload",
    [
        UserPatch(user_phone=None),
        UserUpdate(user_name="unchanged", status="1", nickname="allowed"),
    ],
)
def test_noop_and_explicit_clear_cannot_skip_field_authorization(payload):
    with pytest.raises(BusinessException) as error:
        validate_user_payload(payload, resolve=resolver)
    assert error.value.error_code == "FIELD_WRITE_DENIED"


def test_absent_field_is_not_a_write():
    validate_user_payload(UserPatch(nickname="allowed"), resolve=resolver)


def test_creation_keeps_relation_replacements_separate_from_profile_and_secret():
    groups = user_write_footprint(
        UserCreate(
            user_name="created",
            password="Password123",
            status="1",
            role_ids=["1"],
            dept_ids=[],
        )
    )
    assert groups[("create", "management.create")] == frozenset(
        {"user_name", "password", "status"}
    )
    assert groups[("replace", "management.relations")] == frozenset(
        {"roles", "departments"}
    )
