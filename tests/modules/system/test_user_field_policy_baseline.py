"""Conservative role candidates are evidence, never effective authorization."""

from dataclasses import FrozenInstanceError, replace

import pytest

from app.core.exceptions import InvalidParameterException
from app.modules.system.field_policy.baseline import (
    RoleBaselineSource,
    build_role_baseline,
)
from app.modules.system.field_policy.documents import canonical_rules, catalog_digest
from app.modules.system.field_policy.evaluator import resolve_record_fields
from app.modules.system.field_policy.user_fields import USER_FIELDS


def source(*permissions, **overrides):
    values = {
        "tenant_id": 0,
        "role_id": 101,
        "role_code": "R_USER",
        "status": "1",
        "permissions": frozenset(permissions),
        "data_scope": "2",
        "dept_ids": (10, 20),
    }
    values.update(overrides)
    return RoleBaselineSource(**values)


def keys(candidate):
    return {(rule.field_id, rule.operation, rule.purpose) for rule in candidate.rules}


def test_empty_super_and_disabled_roles_never_mint_field_grants():
    assert not build_role_baseline(source()).rules
    assert not build_role_baseline(source(role_code="R_SUPER")).rules
    disabled = build_role_baseline(source("system:user:list", status="2"))
    assert not disabled.rules
    assert disabled.review_notes


def test_list_maps_actual_dto_and_keeps_sensitive_queries_ungranted():
    candidate = build_role_baseline(source("system:user:list"))
    reads = {
        rule.field_id: rule for rule in candidate.rules if rule.operation == "read"
    }
    assert set(reads) == {
        "user_name",
        "nickname",
        "user_phone",
        "user_email",
        "user_gender",
        "status",
        "create_time",
        "roles",
        "departments",
    }
    assert reads["user_phone"].read == "masked"
    assert reads["user_phone"].mask_id == "phone/v1"
    assert reads["user_email"].mask_id == "email/v1"
    assert all(
        rule.field_id not in {"user_phone", "user_email"}
        for rule in candidate.rules
        if rule.purpose == "management.query"
    )
    assert not any(rule.operation in {"group", "distinct"} for rule in candidate.rules)


def test_list_query_mapping_is_limited_to_existing_rest_filters():
    candidate = build_role_baseline(source("system:user:list"))
    assert {
        (rule.field_id, rule.operation)
        for rule in candidate.rules
        if rule.purpose == "management.query"
    } == {
        ("user_name", "contains"),
        ("nickname", "contains"),
        ("status", "eq"),
        ("user_gender", "eq"),
        ("roles", "eq"),
    }


@pytest.mark.parametrize(
    "permission,operation,purpose,fields",
    [
        (
            "system:user:add",
            "create",
            "management.create",
            {
                "user_name",
                "nickname",
                "user_phone",
                "user_email",
                "user_gender",
                "status",
                "password",
            },
        ),
        (
            "system:user:edit",
            "update",
            "management.update",
            {
                "user_name",
                "nickname",
                "user_phone",
                "user_email",
                "user_gender",
                "status",
            },
        ),
        ("system:user:reset-password", "update", "management.update", {"password"}),
    ],
)
def test_writes_follow_real_input_schemas(permission, operation, purpose, fields):
    assert keys(build_role_baseline(source(permission))) == {
        (field, operation, purpose) for field in fields
    }


def test_relation_permission_requires_both_codes_on_this_role():
    assert not build_role_baseline(source("system:user:role-auth")).rules
    candidate = build_role_baseline(
        source(
            "system:user:edit",
            "system:user:role-auth",
            "system:dept:list",
        )
    )
    assert ("roles", "replace", "management.relations") in keys(candidate)
    assert ("departments", "replace", "management.relations") in keys(candidate)
    assert not any(
        rule.operation == "replace"
        for rule in build_role_baseline(
            source("system:user:add", "system:user:role-auth", "system:dept:list")
        ).rules
    )


def test_import_requires_department_permission_and_uses_only_file_inputs():
    assert not build_role_baseline(source("system:user:import")).rules
    candidate = build_role_baseline(source("system:user:import", "system:dept:list"))
    assert ("employee_no", "exact", "import") in keys(candidate)
    assert ("user_name", "exact", "import") in keys(candidate)
    assert ("departments", "replace", "import") in keys(candidate)
    assert not any(rule.field_id in {"roles", "password"} for rule in candidate.rules)
    assert all(rule.purpose == "import" for rule in candidate.rules)
    with_roles = build_role_baseline(
        source(
            "system:user:import",
            "system:dept:list",
            "system:user:role-auth",
        )
    )
    assert ("roles", "replace", "import") in keys(with_roles)


def test_export_does_not_synthesize_read_or_escalate_existing_mask():
    export_only = build_role_baseline(source("system:user:export"))
    assert all(rule.purpose == "export" for rule in export_only.rules)
    assert {rule.field_id for rule in export_only.rules} == {
        "user_name",
        "nickname",
        "user_phone",
        "user_email",
        "user_gender",
        "status",
        "create_time",
        "roles",
        "departments",
    }
    combined = build_role_baseline(source("system:user:export", "system:user:list"))
    decisions = resolve_record_fields(
        catalog=USER_FIELDS,
        trusted_grants=combined.rules,
        applicable_role_ids=frozenset({"101"}),
        operation="export",
        purpose="export",
        entrypoint="export",
    )
    assert decisions.fields["user_email"].read == "masked"
    assert combined.review_notes


def test_per_role_rules_cannot_borrow_other_roles_scope():
    broad = build_role_baseline(
        source("system:user:export", data_scope="1", dept_ids=())
    )
    narrow = build_role_baseline(source("system:user:list", role_id=102))
    decision = resolve_record_fields(
        catalog=USER_FIELDS,
        trusted_grants=broad.rules + narrow.rules,
        applicable_role_ids=frozenset({"101"}),
        operation="export",
        purpose="export",
        entrypoint="export",
    )
    assert not decision.fields["user_email"].allowed


def test_ai_entry_permission_is_not_field_authority():
    candidate = build_role_baseline(source("ai:chat:use"))
    assert not candidate.rules
    assert not any(rule.purpose == "own-account" for rule in candidate.rules)


def test_candidate_is_immutable_deterministic_and_bound_to_source():
    original = source("system:user:list", "system:user:export")
    candidate = build_role_baseline(original)
    assert candidate == build_role_baseline(original)
    assert candidate.template_revision == "legacy-user-fields/v1"
    assert candidate.catalog_digest == catalog_digest()
    assert len(candidate.source_digest) == 64
    with pytest.raises(FrozenInstanceError):
        candidate.rules = ()
    with pytest.raises(FrozenInstanceError):
        original.role_id = 202
    for changes in (
        {"tenant_id": 1},
        {"role_id": 102},
        {"role_code": "R_OTHER"},
        {"status": "2"},
        {"permissions": frozenset()},
        {"data_scope": "1"},
        {"dept_ids": (30,)},
    ):
        assert (
            build_role_baseline(replace(original, **changes)).source_digest
            != candidate.source_digest
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": True},
        {"tenant_id": -1},
        {"tenant_id": 2**63},
        {"role_id": False},
        {"role_id": 0},
        {"role_id": 2**63},
        {"role_code": ""},
        {"role_code": "x" * 51},
        {"role_code": "R\nOTHER"},
        {"status": "enabled"},
        {"data_scope": "6"},
        {"permissions": {"system:user:list"}},
        {"permissions": frozenset({"*"})},
        {"permissions": frozenset({1})},
        {"permissions": frozenset({"x" * 129})},
        {"permissions": frozenset(f"system:user:p{i}" for i in range(1025))},
        {"dept_ids": [10]},
        {"dept_ids": (False,)},
        {"dept_ids": (0,)},
        {"dept_ids": (20, 10)},
        {"dept_ids": (10, 10)},
        {"dept_ids": tuple(range(1, 1026))},
    ],
)
def test_invalid_or_over_budget_sources_are_rejected_without_echo(changes):
    with pytest.raises(InvalidParameterException) as error:
        build_role_baseline(source(**changes))
    assert error.value.error_code == "FIELD_POLICY_INVALID"
    assert "R\nOTHER" not in str(error.value)


def test_arbitrary_source_object_is_rejected():
    with pytest.raises(InvalidParameterException):
        build_role_baseline({"tenant_id": 0})


def test_source_byte_budget_is_enforced():
    permissions = frozenset(f"system:user:{'a' * 100}{i}" for i in range(600))
    with pytest.raises(InvalidParameterException):
        build_role_baseline(source(permissions=permissions))


def test_all_generated_rules_pass_storage_catalog_validation_without_duplicates():
    candidate = build_role_baseline(
        source(
            "system:user:list",
            "system:user:add",
            "system:user:edit",
            "system:user:reset-password",
            "system:user:role-auth",
            "system:dept:list",
            "system:user:import",
            "system:user:export",
            "ai:chat:use",
        )
    )
    documents = canonical_rules(candidate.rules, role_id=101)
    assert len(documents) == len(keys(candidate)) <= 256
    assert not any(
        rule.field_id
        in {
            "user_avatar",
            "update_time",
            "email_verified",
            "hashed_password",
            "auth_version",
            "tenant_id",
            "email_verified_at",
        }
        for rule in candidate.rules
    )


def test_unknown_valid_permission_does_not_generate_future_field_grants():
    candidate = build_role_baseline(source("system:user:future-capability"))
    assert not candidate.rules
