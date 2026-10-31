from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.core.exceptions import AuthorizationException
from app.modules.system.field_policy.evaluator import FieldGrant, resolve_record_fields
from app.modules.system.field_policy.transfer import (
    build_export_workbook,
    import_query_plan,
    import_write_plan,
    make_lineage,
    project_export,
    safe_failed_rows,
    validate_lineage,
    verify_bytes,
)
from app.modules.system.field_policy.user_fields import USER_FIELDS
from app.modules.system.schemas.user_transfer import FailedRow, UserImportRecord


def test_import_exact_dependency_has_its_own_purpose():

    plan = import_query_plan(employee_no=True)
    assert {(d.field_id, d.operation, d.purpose) for d in plan.dependencies} == {
        ("user_name", "exact", "import"),
        ("employee_no", "exact", "import"),
    }


def test_import_footprint_includes_carried_empty_and_implicit_defaults():

    row = UserImportRecord(
        row_num=2, user_name="someone", dept_input="Engineering", nickname=None
    )
    plan = import_write_plan(row, creating=False, rename=False)
    assert plan["update"] == frozenset({"nickname", "user_gender", "status"})
    assert plan["replace"] == frozenset({"departments"})
    assert {"password", "user_name", "employee_no"} <= import_write_plan(
        row, creating=True, rename=False
    )["create"]


def test_projection_intersects_read_and_export_and_never_writes_hidden_values():

    grants = tuple(
        FieldGrant("r", f, op, purpose, **kw)
        for f, op, purpose, kw in (
            ("user_phone", "export", "export", {}),
            (
                "user_phone",
                "read",
                "management.read",
                {"read": "masked", "mask_id": "phone/v1"},
            ),
            ("user_name", "export", "export", {}),
            ("user_name", "read", "management.read", {}),
            ("roles", "export", "export", {}),
        )
    )
    decisions = resolve_record_fields(
        catalog=USER_FIELDS,
        trusted_grants=grants,
        applicable_role_ids=frozenset({"r"}),
        operation="export",
        purpose="export",
        entrypoint="export",
    )
    projection = project_export(
        (
            {
                "user_name": "=HYPERLINK(secret)",
                "user_phone": "13812345678",
                "roles": "secret-role",
            },
        ),
        (decisions,),
    )
    data = build_export_workbook(projection)
    wb = load_workbook(BytesIO(data))
    cells = [c for ws in wb for row in ws for c in row]
    assert "13812345678" not in str([c.value for c in cells])
    assert "secret-role" not in str([c.value for c in cells])
    assert "138****5678" in [c.value for c in cells]
    assert all(c.data_type != "f" for c in cells)
    wb.close()


def test_error_projection_drops_unproven_values_and_free_text():

    result = safe_failed_rows(
        [
            FailedRow(
                row_num=2,
                field="user_name",
                value="secret-user",
                reason="Duplicate secret-user in secret-dept",
                error_code="AI_IMPORT_USERNAME_DUPLICATE",
            )
        ]
    )
    assert result[0].row_num == 2
    assert result[0].error_code == "AI_IMPORT_USERNAME_DUPLICATE"
    assert "secret" not in result[0].model_dump_json()


def test_lineage_requires_exact_identity_stamp_and_file_bytes():

    stamp = {
        "global_epoch": 1,
        "tenant_epoch": 2,
        "mode": "legacy",
        "active_digest": None,
        "catalog_revision": "test",
        "catalog_digest": "a" * 64,
        "build": "development",
    }
    lineage = make_lineage(
        tenant_id=0,
        owner_id=12,
        resource_id="resource",
        kind="export",
        stamp=stamp,
        data=b"file",
        columns=("user_name",),
        sources=(("55", ("plain",)),),
    )
    validate_lineage(
        lineage,
        tenant_id=0,
        owner_id=12,
        resource_id="resource",
        kind="export",
        stamp=stamp,
    )
    for changed in (
        {"tenant_id": 1},
        {"owner_id": 13},
        {"resource_id": "other"},
        {"stamp": {**stamp, "tenant_epoch": 3}},
    ):
        with pytest.raises(AuthorizationException):
            validate_lineage(
                lineage,
                **(
                    {
                        "tenant_id": 0,
                        "owner_id": 12,
                        "resource_id": "resource",
                        "kind": "export",
                        "stamp": stamp,
                    }
                    | changed
                ),
            )
    with pytest.raises(AuthorizationException):
        verify_bytes(lineage, b"tampered")
    with pytest.raises(AuthorizationException):
        validate_lineage(
            None,
            tenant_id=0,
            owner_id=12,
            resource_id="resource",
            kind="export",
            stamp=stamp,
        )
