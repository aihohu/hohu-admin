import pytest
from pydantic import ValidationError

from app.main import app
from app.modules.system.schemas.user_projection import ProjectedUserOut


def test_response_validation_preserves_omission_and_real_null():
    dto = ProjectedUserOut.model_validate(
        {
            "userId": "101",
            "nickname": None,
            "fieldAccess": {"userPhone": {"read": "hidden", "maskId": None}},
        }
    )
    value = dto.model_dump(mode="json", by_alias=True)
    assert value == {
        "userId": "101",
        "nickname": None,
        "fieldAccess": {"userPhone": {"read": "hidden", "maskId": None}},
    }
    with pytest.raises(ValidationError):
        ProjectedUserOut.model_validate({"userId": "101", "hashedPassword": "secret"})


def test_patch_and_own_capabilities_are_real_registered_routes():
    paths = app.openapi()["paths"]
    assert "patch" in paths["/system/user/{user_id}"]
    assert "put" in paths["/system/user/{user_id}"]
    assert "get" in paths["/system/user/field-capabilities"]
