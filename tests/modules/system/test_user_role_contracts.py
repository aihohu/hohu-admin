"""Phase 2-B user role request-contract tests."""

import io

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from app.modules.system.schemas.user import (
    UserCreate,
    UserDepartmentUpdate,
    UserRoleUpdate,
    UserUpdate,
)
from app.modules.system.service.user_import_parser import (
    MIME_CSV,
    MIME_XLSX,
    import_file_has_column,
)


def _create_payload() -> dict:
    return {
        "userName": "phase2user",
        "nickname": "Phase Two",
        "password": "Phase2Pass123",
        "status": "1",
    }


def test_profile_update_rejects_legacy_role_and_department_fields() -> None:
    for extra_field, value in (
        ("roles", ["R_USER"]),
        ("deptIds", []),
        ("password", "IgnoredPass123"),
    ):
        with pytest.raises(ValidationError) as exc_info:
            UserUpdate.model_validate(
                {
                    "userName": "phase2user",
                    "status": "1",
                    extra_field: value,
                }
            )

        assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


def test_create_distinguishes_omitted_and_explicit_role_ids() -> None:
    implicit = UserCreate.model_validate(_create_payload())
    explicit_empty = UserCreate.model_validate({**_create_payload(), "roleIds": []})
    explicit = UserCreate.model_validate(
        {**_create_payload(), "roleIds": ["9007199254740993"]}
    )

    assert implicit.role_ids is None
    assert explicit_empty.role_ids == []
    assert explicit.role_ids == ["9007199254740993"]


def test_create_rejects_explicit_null_and_numeric_role_ids() -> None:
    for invalid_role_ids in (None, [9007199254740993], ["0"], ["01"]):
        with pytest.raises(ValidationError):
            UserCreate.model_validate(
                {**_create_payload(), "roleIds": invalid_role_ids}
            )


def test_create_rejects_legacy_roles_field() -> None:
    with pytest.raises(ValidationError) as exc_info:
        UserCreate.model_validate({**_create_payload(), "roles": ["R_USER"]})

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


def test_role_update_requires_a_unique_non_empty_complete_set() -> None:
    valid = UserRoleUpdate.model_validate(
        {"roleIds": ["9007199254740993", "9007199254740994"]}
    )
    assert valid.role_ids == ["9007199254740993", "9007199254740994"]

    for invalid in ([], ["1", "1"], [1], ["0"], ["01"]):
        with pytest.raises(ValidationError):
            UserRoleUpdate.model_validate({"roleIds": invalid})


def test_role_request_json_schema_uses_snowflake_strings() -> None:
    create_role_ids = UserCreate.model_json_schema(by_alias=True)["properties"][
        "roleIds"
    ]
    update_role_ids = UserRoleUpdate.model_json_schema(by_alias=True)["properties"][
        "roleIds"
    ]

    create_array_schema = next(
        item for item in create_role_ids["anyOf"] if item.get("type") == "array"
    )
    assert create_array_schema["items"]["type"] == "string"
    assert update_role_ids["items"]["type"] == "string"


def test_department_update_accepts_only_the_canonical_complete_contract() -> None:
    valid = UserDepartmentUpdate.model_validate(
        {
            "deptAssignments": [
                {"deptId": "9007199254740993", "isPrimary": True},
                {"deptId": "9007199254740994", "isPrimary": False},
            ]
        }
    )

    assert [item.dept_id for item in valid.dept_assignments] == [
        "9007199254740993",
        "9007199254740994",
    ]
    assert valid.dept_assignments[0].is_primary is True
    schema = UserDepartmentUpdate.model_json_schema(by_alias=True)
    assignment = schema["$defs"]["UserDepartmentAssignment"]
    assert assignment["properties"]["deptId"]["type"] == "string"
    assert assignment["properties"]["isPrimary"]["type"] == "boolean"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"deptAssignments": None},
        {"deptAssignments": [{"deptId": 9007199254740993, "isPrimary": True}]},
        {"deptAssignments": [{"deptId": "0", "isPrimary": True}]},
        {"deptAssignments": [{"deptId": "01", "isPrimary": True}]},
        {"deptAssignments": [{"deptId": "1", "isPrimary": 1}]},
        {
            "deptAssignments": [
                {"deptId": "1", "isPrimary": True},
                {"deptId": "1", "isPrimary": False},
            ]
        },
        {"deptAssignments": [{"deptId": "1", "isPrimary": True, "legacy": "ignored"}]},
        {"deptAssignments": [], "legacy": "ignored"},
    ],
)
def test_department_update_rejects_non_canonical_or_ambiguous_payloads(
    payload: dict,
) -> None:
    with pytest.raises(ValidationError):
        UserDepartmentUpdate.model_validate(payload)


def test_import_role_column_presence_does_not_depend_on_cell_values() -> None:
    with_empty_role_column = (
        b"user_name,dept_input,role_input,status\nphase2user,QA,,1\n"
    )
    without_role_column = b"user_name,dept_input,status\nphase2user,QA,1\n"

    assert (
        import_file_has_column(with_empty_role_column, MIME_CSV, "role_input") is True
    )
    assert import_file_has_column(without_role_column, MIME_CSV, "role_input") is False


def test_xlsx_role_column_presence_does_not_depend_on_cell_values() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["user_name", "dept_input", "role_input", "status"])
    worksheet.append(["phase2user", "QA", "", "1"])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()

    assert import_file_has_column(output.getvalue(), MIME_XLSX, "role_input") is True
