"""No field value or relation label may outlive its projection decision."""

import pytest

from app.core.exceptions import BusinessException
from app.modules.system.field_policy.dto import (
    USER_OUTPUT_SOURCES,
    project_user_dto,
    resolve_own_account_fields,
)
from app.modules.system.field_policy.evaluator import FieldGrant, resolve_record_fields
from app.modules.system.field_policy.user_fields import USER_FIELDS


def decisions(*grants):
    return resolve_record_fields(
        catalog=USER_FIELDS,
        trusted_grants=grants,
        applicable_role_ids=frozenset({"1"}),
        operation="read",
        purpose="management.read",
        entrypoint="user-list",
    )


def read(field, **kwargs):
    return FieldGrant("1", field, "read", "management.read", **kwargs)


def test_own_profile_roles_rejects_unregistered_nested_values():
    own = resolve_own_account_fields(
        actor=(0, 1),
        target=(0, 1),
        grants=(),
        applicable_role_ids=frozenset(),
        operation="read",
    )
    with pytest.raises(BusinessException):
        project_user_dto(
            view="profile",
            values={"roles": {"hashed_password": "secret"}},
            decisions=own,
        )


def test_own_account_rejects_boolean_identity_alias():
    with pytest.raises(BusinessException):
        resolve_own_account_fields(
            actor=(0, 1),
            target=(0, True),
            grants=(),
            applicable_role_ids=frozenset(),
            operation="read",
        )


def test_dto_omits_hidden_keeps_actual_null_and_caps_masked():
    dto = project_user_dto(
        view="list",
        values={
            "user_id": 123,
            "nickname": None,
            "user_phone": "13812341234",
            "user_email": "private@example.com",
        },
        decisions=decisions(read("nickname"), read("user_phone")),
    )
    assert dto["userId"] == "123"
    assert dto["nickname"] is None
    assert dto["userPhone"] == "138****1234"
    assert "userEmail" not in dto
    assert dto["fieldAccess"]["userEmail"]["read"] == "hidden"
    assert "private" not in str(dto)


def test_hidden_relation_hides_every_dependent_label_and_identifier():
    dto = project_user_dto(
        view="list",
        values={
            "user_id": 123,
            "roles": {
                "roles": ["R_SECRET"],
                "role_names": ["private name"],
                "role_name_keys": [],
            },
            "departments": {
                "dept_ids": ["456"],
                "dept_names": "private dept",
                "primary_dept": "456",
                "user_depts": [],
            },
        },
        decisions=decisions(),
    )
    assert not (
        {
            "roles",
            "roleNames",
            "roleNameKeys",
            "deptIds",
            "deptNames",
            "primaryDept",
            "userDepts",
        }
        & dto.keys()
    )
    assert "private" not in str(dto)


def test_unknown_nested_relation_field_cannot_escape_through_allowed_root():
    with pytest.raises(BusinessException):
        project_user_dto(
            view="list",
            values={
                "user_id": 123,
                "roles": {"roles": ["R_USER"], "hashed_password": "secret"},
            },
            decisions=decisions(read("roles")),
        )


def test_own_account_baseline_is_self_only_and_explicit_deny_wins():
    own = resolve_own_account_fields(
        actor=(0, 1),
        target=(0, 1),
        grants=(),
        applicable_role_ids=frozenset(),
        operation="read",
    )
    assert own.fields["user_email"].allowed
    assert not own.fields["hashed_password"].allowed
    deny = FieldGrant("1", "user_email", "read", "own-account", effect="deny")
    restricted = resolve_own_account_fields(
        actor=(0, 1),
        target=(0, 1),
        grants=(deny,),
        applicable_role_ids=frozenset({"1"}),
        operation="read",
    )
    assert not restricted.fields["user_email"].allowed
    for target in ((1, 1), (0, 2)):
        with pytest.raises(BusinessException):
            resolve_own_account_fields(
                actor=(0, 1),
                target=target,
                grants=(),
                applicable_role_ids=frozenset(),
                operation="read",
            )


def test_every_wire_field_has_a_canonical_source_and_no_secret_source():
    for fields in USER_OUTPUT_SOURCES.values():
        for source in fields.values():
            assert source.field_id in USER_FIELDS
            assert USER_FIELDS[source.field_id].readable
            assert source.tables


def test_member_flags_are_omitted_with_the_department_relation():
    dto = project_user_dto(
        view="members",
        values={"user_id": 123, "departments": {"is_member": True, "is_primary": True}},
        decisions=decisions(),
    )
    assert "isMember" not in dto and "isPrimary" not in dto


def test_write_only_capability_never_reintroduces_a_hidden_value():
    update = resolve_record_fields(
        catalog=USER_FIELDS,
        trusted_grants=(FieldGrant("1", "user_phone", "update", "management.update"),),
        applicable_role_ids=frozenset({"1"}),
        operation="update",
        purpose="management.update",
        entrypoint="user-list",
    )
    dto = project_user_dto(
        view="list",
        values={"user_id": 123, "user_phone": "13812341234"},
        decisions=decisions(),
        capability_decisions=(update,),
        function_permissions=frozenset({"system:user:edit"}),
    )
    assert "userPhone" not in dto
    assert dto["fieldAccess"]["userPhone"] == {
        "read": "hidden",
        "maskId": None,
        "create": False,
        "update": True,
        "replace": False,
        "queryOps": [],
    }


def test_field_capability_cannot_exceed_function_permission():
    update = resolve_record_fields(
        catalog=USER_FIELDS,
        trusted_grants=(FieldGrant("1", "nickname", "update", "management.update"),),
        applicable_role_ids=frozenset({"1"}),
        operation="update",
        purpose="management.update",
        entrypoint="user-list",
    )
    dto = project_user_dto(
        view="list",
        values={"user_id": 123},
        decisions=decisions(),
        capability_decisions=(update,),
        function_permissions=frozenset(),
    )
    assert dto["fieldAccess"]["nickname"]["update"] is False
