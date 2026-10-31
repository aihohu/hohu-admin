"""Persisted policy documents must reject unsupported or ambiguous rules."""

import copy

import pytest

from app.core.exceptions import InvalidParameterException
from app.modules.system.field_policy.documents import (
    canonical_rules,
    catalog_digest,
    read_rules,
    rules_digest,
)
from app.modules.system.field_policy.evaluator import FieldGrant


def test_policy_document_canonicalizes_order_and_round_trips():
    grants = (
        FieldGrant(
            "17",
            "user_phone",
            "read",
            "management.read",
            read="masked",
            mask_id="phone/v1",
        ),
        FieldGrant("17", "nickname", "update", "management.update"),
    )
    document = canonical_rules(grants, role_id=17)
    assert [rule["field_id"] for rule in document] == ["nickname", "user_phone"]
    assert read_rules(document, role_id=17) == tuple(reversed(grants))
    assert rules_digest(document) == rules_digest(
        canonical_rules(tuple(reversed(grants)), role_id=17)
    )
    assert len(catalog_digest()) == 64


@pytest.mark.parametrize(
    "grant",
    [
        FieldGrant("17", "password", "read", "management.read"),
        FieldGrant("17", "employee_no", "exact", "management.query"),
        FieldGrant("17", "user_avatar", "update", "management.update"),
        FieldGrant("17", "roles", "update", "management.update"),
        FieldGrant("17", "user_id", "create", "management.create"),
        FieldGrant("18", "nickname", "read", "management.read"),
    ],
)
def test_policy_document_rejects_catalog_unsupported_or_wrong_role(grant):
    with pytest.raises(InvalidParameterException) as exc:
        canonical_rules((grant,), role_id=17)
    assert exc.value.error_code == "FIELD_POLICY_INVALID"


def test_policy_document_rejects_duplicate_and_conflicting_rules():
    allow = FieldGrant("17", "nickname", "read", "management.read")
    deny = FieldGrant("17", "nickname", "read", "management.read", effect="deny")
    for rules in ((allow, allow), (allow, deny)):
        with pytest.raises(InvalidParameterException):
            canonical_rules(rules, role_id=17)


@pytest.mark.parametrize("mutation", ["extra", "missing", "bad_type", "non_list"])
def test_persisted_rules_are_validated_without_echoing_values(mutation):
    document = canonical_rules(
        (FieldGrant("17", "nickname", "read", "management.read"),), role_id=17
    )
    value = copy.deepcopy(document)
    if mutation == "extra":
        value[0]["secret"] = "do-not-echo"
    elif mutation == "missing":
        del value[0]["effect"]
    elif mutation == "bad_type":
        value[0]["field_id"] = ["do-not-echo"]
    else:
        value = {"do-not-echo": document}
    with pytest.raises(InvalidParameterException) as exc:
        read_rules(value, role_id=17)
    assert "do-not-echo" not in str(exc.value)


def test_policy_document_budget_and_empty_policy():
    assert canonical_rules((), role_id=17) == []
    assert read_rules([], role_id=17) == ()
    grant = FieldGrant("17", "nickname", "read", "management.read")
    with pytest.raises(InvalidParameterException):
        canonical_rules((grant,) * 257, role_id=17)
