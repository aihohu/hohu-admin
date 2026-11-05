"""Pure user-field rules: no routes, database, model calls or activation."""

from dataclasses import FrozenInstanceError, replace

import pytest

from app.core.exceptions import AuthorizationException, InvalidParameterException
from app.modules.system.field_policy.evaluator import (
    FieldGrant,
    project_user_values,
    resolve_record_fields,
    validate_write_fields,
)
from app.modules.system.field_policy.user_fields import USER_FIELDS


def grant(field, *, role="names", operation="read", purpose="management.read", **kw):
    return FieldGrant(
        role_id=role,
        field_id=field,
        operation=operation,
        purpose=purpose,
        **kw,
    )


def resolve(grants=(), *, roles=("names", "finance"), **kw):
    return resolve_record_fields(
        catalog=USER_FIELDS,
        trusted_grants=tuple(grants),
        applicable_role_ids=frozenset(roles),
        operation=kw.pop("operation", "read"),
        purpose=kw.pop("purpose", "management.read"),
        entrypoint=kw.pop("entrypoint", "user-list"),
        **kw,
    )


def test_role_field_grant_does_not_borrow_another_roles_row_scope():
    grants = [grant("user_name"), grant("user_email", role="finance")]
    other_department = resolve(grants, roles=("names",))
    finance_department = resolve(grants)
    assert other_department.fields["user_name"].read == "plain"
    assert other_department.fields["user_email"].read == "hidden"
    assert finance_department.fields["user_email"].read == "plain"


def test_no_grant_is_neutral_but_applicable_explicit_deny_wins():
    allow = grant("user_email", role="finance")
    deny = grant("user_email", effect="deny")
    assert resolve([allow]).fields["user_email"].allowed
    assert not resolve([allow, deny]).fields["user_email"].allowed
    assert resolve([allow, deny], roles=("finance",)).fields["user_email"].allowed
    assert not resolve([allow], roles=()).fields["user_email"].allowed


def test_super_role_has_no_implicit_field_bypass():
    assert resolve(roles=("R_SUPER",)).fields["user_email"].read == "hidden"


def test_purpose_is_bound_to_each_grant():
    own = grant("user_email", purpose="own-account")
    assert resolve([own]).fields["user_email"].read == "hidden"
    assert resolve([own], purpose="own-account").fields["user_email"].allowed


def test_plain_and_masked_merge_without_creating_write_or_query_authority():
    grants = [
        grant("user_phone", read="masked", mask_id="phone/v1"),
        grant("user_phone", role="finance"),
    ]
    assert resolve(grants).fields["user_phone"].read == "plain"
    assert resolve(grants, roles=("names",)).fields["user_phone"].read == "masked"
    for op, purpose in [("update", "management.update"), ("exact", "management.query")]:
        assert (
            not resolve(grants, operation=op, purpose=purpose)
            .fields["user_phone"]
            .allowed
        )


def test_projection_caps_limit_plain_and_preserve_hidden_vs_real_null():
    decisions = resolve([grant("user_phone"), grant("nickname"), grant("user_gender")])
    raw = {
        "user_id": 191000000000000001,
        "user_phone": "13812345678",
        "nickname": None,
        "user_gender": "1",
        "user_email": "private@example.com",
    }
    result = project_user_values(
        raw_values=raw,
        decisions=decisions,
        entrypoint_caps={
            "user_id": "plain",
            "user_phone": "plain",
            "nickname": "plain",
            "user_email": "plain",
        },
    )
    assert dict(result.values) == {
        "user_id": "191000000000000001",
        "user_phone": "138****5678",
        "nickname": None,
    }
    assert result.field_access["user_phone"].read == "masked"
    assert result.field_access["user_email"].read == "hidden"
    assert "user_gender" not in result.values
    assert "user_gender" not in result.field_access
    assert raw["user_phone"] == "13812345678"


def test_masked_null_is_not_changed_to_empty_string():
    result = project_user_values(
        raw_values={"user_phone": None},
        decisions=resolve([grant("user_phone", read="masked", mask_id="phone/v1")]),
        entrypoint_caps={"user_phone": "plain"},
    )
    assert result.values["user_phone"] is None


@pytest.mark.parametrize(
    "field,value,mask",
    [
        ("user_phone", "1234", "phone/v1"),
        ("user_email", "a@example.com", "email/v1"),
        ("user_email", "malformed", "email/v1"),
    ],
)
def test_malformed_or_short_legacy_values_never_fall_back_to_plain(field, value, mask):
    result = project_user_values(
        raw_values={field: value},
        decisions=resolve([grant(field, read="masked", mask_id=mask)]),
        entrypoint_caps={field: "masked"},
    )
    assert result.values[field] != value
    assert "*" in result.values[field]


def test_export_requires_separate_grant_and_can_be_further_masked():
    read = grant("user_email")
    export = grant("user_email", operation="export", purpose="export")
    assert (
        not resolve([read], operation="export", purpose="export")
        .fields["user_email"]
        .allowed
    )
    decisions = resolve(
        [read, export], operation="export", purpose="export", entrypoint="export"
    )
    result = project_user_values(
        raw_values={"user_email": "alice@example.com"},
        decisions=decisions,
        entrypoint_caps={"user_email": "masked"},
    )
    assert result.values["user_email"] == "a**e@example.com"


@pytest.mark.parametrize("read_mode", ["absent", "deny", "masked", "outside-row"])
def test_export_never_restores_revoked_or_stronger_read_access(read_mode):
    grants = [grant("user_email", operation="export", purpose="export")]
    if read_mode == "deny":
        grants.extend(
            [grant("user_email"), grant("user_email", role="finance", effect="deny")]
        )
    elif read_mode == "masked":
        grants.append(grant("user_email", read="masked", mask_id="email/v1"))
    elif read_mode == "outside-row":
        grants.append(grant("user_email", role="other-department"))
    decisions = resolve(
        grants, operation="export", purpose="export", entrypoint="export"
    )
    expected = "masked" if read_mode == "masked" else "hidden"
    assert decisions.fields["user_email"].read == expected
    result = project_user_values(
        raw_values={"user_email": "alice@example.com"},
        decisions=decisions,
        entrypoint_caps={"user_email": "plain"},
    )
    if read_mode == "masked":
        assert result.values["user_email"] == "a**e@example.com"
    else:
        assert "user_email" not in result.values


@pytest.mark.parametrize(
    "field",
    ["password", "hashed_password", "auth_version", "tenant_id", "email_verified_at"],
)
def test_secret_and_internal_fields_never_project_even_with_plain_grant(field):
    result = project_user_values(
        raw_values={field: "must-not-escape"},
        decisions=resolve([grant(field)]),
        entrypoint_caps={field: "plain"},
    )
    assert field not in result.values
    assert "must-not-escape" not in repr(result)


def test_write_only_password_allows_explicit_create_without_reading_secret():
    decisions = resolve(
        [grant("password", operation="create", purpose="management.create")],
        operation="create",
        purpose="management.create",
    )
    validate_write_fields(
        supplied_paths=frozenset({"password"}), decisions=decisions, operation="create"
    )
    assert decisions.fields["password"].read == "hidden"


def test_patch_presence_checks_explicit_null_and_unchanged_submitted_fields():
    decisions = resolve(
        [grant("nickname", operation="update", purpose="management.update")],
        operation="update",
        purpose="management.update",
    )
    validate_write_fields(
        supplied_paths=frozenset(), decisions=decisions, operation="update"
    )
    validate_write_fields(
        supplied_paths=frozenset({"nickname"}), decisions=decisions, operation="update"
    )
    for supplied in (
        {"user_email": None},
        {"nickname": "ok", "user_email": "unchanged@example.com"},
    ):
        before = supplied.copy()
        with pytest.raises(AuthorizationException) as exc:
            validate_write_fields(
                supplied_paths=frozenset(supplied),
                decisions=decisions,
                operation="update",
            )
        assert exc.value.error_code == "FIELD_WRITE_DENIED"
        assert supplied == before
        assert "unchanged@example.com" not in str(exc.value)


@pytest.mark.parametrize(
    "path",
    [
        "unknown",
        "nickname.child",
        "roles.0.role_id",
        "userEmail",
        "tenant_id",
        "create_time",
    ],
)
def test_write_paths_unknown_nested_alias_internal_and_readonly_fail_closed(path):
    decisions = resolve(
        [grant("nickname", operation="update", purpose="management.update")],
        operation="update",
        purpose="management.update",
    )
    with pytest.raises(AuthorizationException):
        validate_write_fields(
            supplied_paths=frozenset({"nickname", path}),
            decisions=decisions,
            operation="update",
        )


def test_read_snapshot_cannot_be_reused_as_write_permission():
    with pytest.raises(InvalidParameterException):
        validate_write_fields(
            supplied_paths=frozenset({"nickname"}),
            decisions=resolve([grant("nickname")]),
            operation="update",
        )


def test_relation_replace_is_independent_and_does_not_grant_nested_fields():
    decisions = resolve(
        [grant("roles", operation="replace", purpose="management.relations")],
        operation="replace",
        purpose="management.relations",
        entrypoint="relations",
    )
    validate_write_fields(
        supplied_paths=frozenset({"roles"}), decisions=decisions, operation="replace"
    )
    assert not decisions.fields["departments"].allowed


def test_catalog_hard_purpose_limits_avatar_employee_and_verified():
    for field, operation, purpose in [
        ("user_avatar", "update", "management.update"),
        ("employee_no", "create", "management.create"),
        ("email_verified", "read", "management.read"),
    ]:
        decisions = resolve(
            [grant(field, operation=operation, purpose=purpose)],
            operation=operation,
            purpose=purpose,
        )
        assert not decisions.fields[field].allowed


def test_employee_number_identity_match_is_limited_to_import_purpose():
    grants = [
        grant("employee_no", operation="exact", purpose=purpose)
        for purpose in ("management.query", "import")
    ]
    assert (
        not resolve(grants, operation="exact", purpose="management.query")
        .fields["employee_no"]
        .allowed
    )
    assert (
        resolve(grants, operation="exact", purpose="import", entrypoint="import")
        .fields["employee_no"]
        .allowed
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"field_id": "surprise"},
        {"operation": "sql"},
        {"purpose": "history"},
        {"effect": "maybe"},
        {"read": "hidden"},
        {"read": "masked", "mask_id": "email/v2"},
    ],
)
def test_unknown_grant_definitions_fail_closed(kwargs):
    args = {
        "role_id": "names",
        "field_id": "user_email",
        "operation": "read",
        "purpose": "management.read",
    }
    args.update(kwargs)
    with pytest.raises(InvalidParameterException):
        resolve([FieldGrant(**args)])


def test_contradictory_catalog_and_invalid_context_fail_closed():
    bad_catalog = dict(USER_FIELDS)
    bad_catalog["nickname"] = replace(USER_FIELDS["nickname"], mask_id="email/v1")
    with pytest.raises(InvalidParameterException):
        resolve_record_fields(
            catalog=bad_catalog,
            trusted_grants=(),
            applicable_role_ids=frozenset(),
            operation="read",
            purpose="management.read",
            entrypoint="user-list",
        )
    with pytest.raises(InvalidParameterException):
        resolve(entrypoint="pretend-admin")


def test_unknown_projection_keys_or_masks_fail_closed():
    decisions = resolve([grant("nickname")])
    for raw, caps in [
        ({"unknown": "sensitive"}, {"nickname": "plain"}),
        ({"nickname": "n"}, {"nickname": "masked"}),
        ({"nickname": "n"}, {"unknown": "plain"}),
    ]:
        with pytest.raises(InvalidParameterException) as exc:
            project_user_values(
                raw_values=raw, decisions=decisions, entrypoint_caps=caps
            )
        assert "sensitive" not in str(exc.value)


def test_decisions_and_nested_projection_are_immutable_and_do_not_alias_input():
    decisions = resolve([grant("roles")])
    with pytest.raises((FrozenInstanceError, AttributeError)):
        decisions.operation = "update"
    with pytest.raises(TypeError):
        decisions.fields["roles"] = decisions.fields["password"]
    raw = {"roles": [{"role_id": "1", "name": "Example"}]}
    result = project_user_values(
        raw_values=raw, decisions=decisions, entrypoint_caps={"roles": "plain"}
    )
    raw["roles"][0]["name"] = "changed"
    assert result.values["roles"][0]["name"] == "Example"
    with pytest.raises(TypeError):
        result.values["roles"][0]["name"] = "changed"


def test_detail_projection_retains_existing_mask_upper_bound():
    result = project_user_values(
        raw_values={"user_phone": "13812345678", "user_email": "alice@example.com"},
        decisions=resolve(
            [grant("user_phone"), grant("user_email")], entrypoint="user-detail"
        ),
        entrypoint_caps={"user_phone": "plain", "user_email": "plain"},
    )
    assert dict(result.values) == {
        "user_phone": "138****5678",
        "user_email": "a**e@example.com",
    }


def test_authorized_row_id_is_a_read_only_anchor_without_implicit_profile_grants():
    read = resolve(roles=(), purpose="own-account", entrypoint="own-profile")
    result = project_user_values(
        raw_values={"user_id": 191000000000000001, "user_email": "private@example.com"},
        decisions=read,
        entrypoint_caps={"user_id": "plain", "user_email": "plain"},
    )
    assert dict(result.values) == {"user_id": "191000000000000001"}
    write = resolve(
        [grant("user_id", operation="update", purpose="management.update")],
        operation="update",
        purpose="management.update",
    )
    with pytest.raises(AuthorizationException):
        validate_write_fields(
            supplied_paths={"user_id"}, decisions=write, operation="update"
        )


@pytest.mark.parametrize("value", [True, -1, "１２３", "not-an-id"])
def test_projection_rejects_invalid_identity_without_echoing_input(value):
    with pytest.raises(InvalidParameterException) as exc:
        project_user_values(
            raw_values={"user_id": value},
            decisions=resolve(),
            entrypoint_caps={"user_id": "plain"},
        )
    assert exc.value.error_code == "FIELD_POLICY_INVALID"
    assert str(value) not in str(exc.value)


@pytest.mark.parametrize("field", ["user_phone", "user_email"])
def test_masking_rejects_non_string_values_without_coercing_sensitive_objects(field):
    with pytest.raises(InvalidParameterException) as exc:
        project_user_values(
            raw_values={field: {"secret": "must-not-escape"}},
            decisions=resolve([grant(field)]),
            entrypoint_caps={field: "masked"},
        )
    assert exc.value.error_code == "FIELD_POLICY_INVALID"
    assert "must-not-escape" not in str(exc.value)


@pytest.mark.parametrize("invalid", [object(), {1: "must-not-escape"}])
def test_nested_relation_projection_rejects_objects_and_invalid_mapping_keys(invalid):
    with pytest.raises(InvalidParameterException) as exc:
        project_user_values(
            raw_values={"roles": [{"details": invalid}]},
            decisions=resolve([grant("roles")]),
            entrypoint_caps={"roles": "plain"},
        )
    assert exc.value.error_code == "FIELD_POLICY_INVALID"
    assert "must-not-escape" not in str(exc.value)


def test_export_deny_cannot_be_overridden_by_plain_source_read():
    decisions = resolve(
        [
            grant("user_email"),
            grant("user_email", operation="export", purpose="export"),
            grant(
                "user_email",
                role="finance",
                operation="export",
                purpose="export",
                effect="deny",
            ),
        ],
        operation="export",
        purpose="export",
        entrypoint="export",
    )
    result = project_user_values(
        raw_values={"user_email": "must-not-escape@example.com"},
        decisions=decisions,
        entrypoint_caps={"user_email": "plain"},
    )
    assert "user_email" not in result.values
    assert result.field_access["user_email"].read == "hidden"
