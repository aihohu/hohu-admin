"""PATCH carries presence; PUT and the AI adapter retain their old contracts."""

import pytest
from pydantic import ValidationError

from app.core.exceptions import BusinessException
from app.modules.system.schemas.user import (
    UpdateProfile,
    UserPatch,
    UserToolUpdate,
    UserUpdate,
)


def test_patch_distinguishes_absence_from_nullable_clear():
    assert UserPatch(nickname="new").model_dump(exclude_unset=True) == {
        "nickname": "new"
    }
    assert UserPatch(userPhone=None).model_dump(exclude_unset=True) == {
        "user_phone": None
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"userName": None},
        {"status": None},
        {},
        {"password": "Password1"},
        {"roles": []},
        {"roleIds": []},
        {"deptIds": []},
        {"userAvatar": "avatar"},
        {"tenantId": "0"},
    ],
)
def test_patch_rejects_required_null_empty_secret_and_relationships(payload):
    with pytest.raises(ValidationError):
        UserPatch.model_validate(payload)


@pytest.mark.parametrize("model", [UserPatch, UserUpdate])
def test_dual_aliases_rejected_even_when_values_are_equal(model):
    with pytest.raises(ValidationError):
        model.model_validate({"userName": "alice", "user_name": "alice", "status": "1"})


@pytest.mark.parametrize("model", [UserPatch, UserUpdate])
@pytest.mark.parametrize(
    "field,value", [("userPhone", "138****1234"), ("userEmail", "a***@example.com")]
)
def test_mask_placeholders_have_a_stable_value_free_error(model, field, value):
    with pytest.raises(BusinessException) as error:
        model.model_validate({"userName": "alice", "status": "1", field: value})
    assert error.value.error_code == "FIELD_MASKED_INPUT_INVALID"
    assert value not in str(error.value)


def test_put_still_requires_identity_and_status_and_ai_none_is_unchanged():
    with pytest.raises(ValidationError):
        UserUpdate(nickname="new")
    model = UserToolUpdate(nickname="new")
    assert model.user_name is None and model.status is None
    assert model.model_dump(exclude_unset=True) == {"nickname": "new"}


@pytest.mark.parametrize(
    "payload",
    [{"unknown": "value"}, {"user_phone": "13812341234", "userPhone": "13812341234"}],
)
def test_own_profile_does_not_ignore_unknown_or_duplicate_input(payload):
    with pytest.raises(ValidationError):
        UpdateProfile.model_validate(payload)


def test_own_profile_rejects_a_mask_instead_of_replacing_the_original():
    with pytest.raises(BusinessException) as error:
        UpdateProfile(user_phone="138****1234")
    assert error.value.error_code == "FIELD_MASKED_INPUT_INVALID"
