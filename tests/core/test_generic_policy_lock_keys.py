"""Typed composite keys retain the existing fixed resource order."""

import pytest

from app.core.authorization_plan import LockPlan, ResourceKey, ResourceKind
from app.core.exceptions import BusinessException


def test_generic_composite_keys_sort_without_coercing_integer_ids():
    a = ResourceKey(ResourceKind.FIELD_POLICY_BINDING, 0, (201, "system.user"))
    b = ResourceKey(ResourceKind.FIELD_POLICY_BINDING, 0, (201, "system.role"))
    c = ResourceKey(ResourceKind.FIELD_POLICY_HEAD, 0, 0)
    assert LockPlan(existing=(a, b, c)).existing == (c, b, a)


@pytest.mark.parametrize(
    "identity",
    [("201", "system.user"), (201, 5), (True, "system.user"), (201, "bad object")],
)
def test_generic_key_rejects_wrong_column_types(identity):
    with pytest.raises(BusinessException):
        ResourceKey(ResourceKind.FIELD_POLICY_BINDING, 0, identity)
