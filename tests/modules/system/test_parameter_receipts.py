"""Protocol receipts never authorize a field and bind the exact live context."""

import pytest

from app.core.exceptions import BusinessException
from app.modules.system.service.parameter_receipt import (
    FormFacts,
    issue_receipt,
    publication_revision,
    require_publication_revision,
    require_receipt,
)


def test_publication_proof_expires_instead_of_being_a_permanent_content_mac():
    row = {
        "tenant_id": 0,
        "config_id": 301,
        "config_key": "business",
        "config_value": "value",
        "config_type": "text",
        "status": "1",
        "is_public": False,
        "value_sensitivity": "private",
    }
    token = publication_revision(facts(), row, None, now=100)
    assert token != publication_revision(facts(), row, None, now=400)
    assert require_publication_revision(token, facts(), row, None, now=399) is None
    for current, context, content, kind in (
        (400, facts(), row, "publish"),
        (97, facts(), row, "publish"),
        (101, facts(b_epoch=3), row, "publish"),
        (101, facts(), row | {"config_value": "changed"}, "publish"),
        (101, facts(), row, "revoke"),
    ):
        with pytest.raises(BusinessException) as denied:
            require_publication_revision(
                token, context, content, None, now=current, kind=kind
            )
        assert denied.value.error_code == "CONFIG_PUBLICATION_CONFLICT"


def facts(**changes):
    return FormFacts(
        **(
            {
                "tenant_id": 0,
                "actor_id": 101,
                "actor_version": 1,
                "tenant_version": 1,
                "object_id": "system.config",
                "mode": "update",
                "target_id": 301,
                "catalog_digest": "a" * 64,
                "g_epoch": 1,
                "b_epoch": 2,
            }
            | changes
        )
    )


def test_receipt_binds_all_current_facts_and_expires_without_reauth_grant():
    token = issue_receipt(facts(), now=100)
    assert require_receipt(token, facts(), now=101) is None
    for change in (
        {"tenant_id": 1},
        {"actor_id": 102},
        {"actor_version": 2},
        {"tenant_version": 2},
        {"target_id": 302},
        {"catalog_digest": "b" * 64},
        {"g_epoch": 2},
        {"b_epoch": 3},
        {"object_id": "job.definition"},
    ):
        with pytest.raises(BusinessException) as denied:
            require_receipt(token, facts(**change), now=101)
        assert (
            denied.value.code == 409
            and denied.value.error_code == "FIELD_POLICY_PROTOCOL_REQUIRED"
        )
    for raw, now in (
        (None, 101),
        (token + "x", 101),
        (token, 400),
        (token, 97),
        ("x" * 2049, 101),
    ):
        with pytest.raises(BusinessException):
            require_receipt(raw, facts(), now=now)


def test_group_receipt_cannot_cross_a_different_settings_group():
    original = facts(
        object_id="system.setting", mode="group", target_id=None, group="brand"
    )
    receipt = issue_receipt(original, now=100)
    with pytest.raises(BusinessException):
        require_receipt(
            receipt,
            facts(
                object_id="system.setting", mode="group", target_id=None, group="files"
            ),
            now=101,
        )
