"""A generic AI receipt binds every object to one current authorization stamp."""

from copy import deepcopy

import pytest

from app.core.exceptions import AuthorizationException
from app.modules.ai.service.delivery_authority import (
    freeze_delivery_authority,
    require_delivery_authority,
    validate_delivery_authority,
)
from tests.modules.ai.test_field_authority import evidence


def receipt(**changes):

    return freeze_delivery_authority(
        **dict(
            tenant_id=71,
            actor_id=701,
            global_epoch=2,
            tenant_epoch=3,
            active_digest="a" * 64,
            build="test-build",
            **changes,
        )
    )


def test_generic_receipt_is_closed_value_free_and_does_not_accept_old_user_stamp():

    value = receipt()
    assert value["schema"] == 2 and value["tenant_id"] == "71"
    assert validate_delivery_authority(value) == value
    assert validate_delivery_authority(evidence()) is None
    assert not {"values", "data", "role_names", "password", "fields"} & value.keys()


@pytest.mark.parametrize(
    "name",
    [
        "tenant_id",
        "actor_id",
        "global_epoch",
        "tenant_epoch",
        "active_digest",
        "catalog_digest",
        "build",
        "digest",
    ],
)
def test_generic_receipt_detects_mutation(name):

    value = deepcopy(receipt())
    value[name] = "tampered"
    assert validate_delivery_authority(value) is None


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": 72},
        {"actor_id": 702},
        {"global_epoch": 9},
        {"tenant_epoch": 10},
        {"active_digest": "b" * 64},
        {"build": "changed"},
    ],
)
def test_all_object_revocation_or_build_change_invalidates_receipt(change):

    original = receipt()
    updated = {
        **{
            "tenant_id": 71,
            "actor_id": 701,
            "global_epoch": 2,
            "tenant_epoch": 3,
            "active_digest": "a" * 64,
            "build": "test-build",
        },
        **change,
    }

    with pytest.raises(AuthorizationException):
        require_delivery_authority(original, freeze_delivery_authority(**updated))
