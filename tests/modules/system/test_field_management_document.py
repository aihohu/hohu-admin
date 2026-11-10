"""UI editing order is independent of immutable persistence order."""

from types import SimpleNamespace

import pytest

from app.core.exceptions import BusinessException
from app.core.field_policy import Catalog
from app.modules.system.field_policy.generic_compat import user_compat_object
from app.modules.system.service.field_management import canonical_draft


def rule(field):
    return {
        "field_id": field,
        "access": {"operation": "read", "purpose": "management.read", "use": ""},
        "effect": "allow",
        "level": "plain",
        "mask_id": None,
    }


def test_management_normalizes_editing_order_without_mutating_the_request():
    catalog = Catalog((user_compat_object(),), ())
    payload = {"rules": [rule("user_name"), rule("nickname")]}
    result = canonical_draft(
        catalog, SimpleNamespace(tenant_id=0), 201, "system.user", payload
    )
    assert [r["field_id"] for r in result] == ["nickname", "user_name"]
    assert payload["rules"][0]["field_id"] == "user_name"


def test_normalization_does_not_silently_collapse_duplicate_rules():
    catalog = Catalog((user_compat_object(),), ())
    with pytest.raises(BusinessException):
        canonical_draft(
            catalog,
            SimpleNamespace(tenant_id=0),
            201,
            "system.user",
            {"rules": [rule("nickname"), rule("nickname")]},
        )
