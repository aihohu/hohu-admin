"""Source domains carry field uses and record sets, never authority or values."""

from copy import deepcopy

import pytest

from app.core.exceptions import BusinessException
from app.modules.ai.service.delivery_authority import validate_delivery_authority
from app.modules.ai.service.delivery_source_domains import source_domain, with_sources
from tests.modules.ai.test_delivery_authority import receipt


def domain(**changes):

    args = {
        "action_id": "ai.tool.role.list",
        "object_id": "system.role",
        "records": (201, 202),
        "read": (("role_name", "plain"),),
        "query": (("status", "eq", "management.query", "filter"),),
    }
    args.update(changes)
    return source_domain(**args)


def test_import_match_is_preserved_as_its_original_registered_use():
    group = source_domain(
        action_id="transfer.user-import",
        object_id="system.user",
        records=(101,),
        query=(("user_name", "exact", "import", "match"),),
    )
    assert group["query"] == [["user_name", "exact", "import", "match"]]


def test_domain_is_canonical_value_free_and_receipt_digest_binds_it():

    value = domain(records=(202, 201, 201))
    assert value["records"] == ["201", "202"]
    assert value == domain()
    proof = with_sources(receipt(), (value,))
    assert proof["source_complete"] and proof["source_domains"] == [value]
    assert validate_delivery_authority(proof) == proof
    altered = deepcopy(proof)
    altered["source_domains"][0]["records"].append("203")
    assert validate_delivery_authority(altered) is None
    assert "role_name" in str(value) and "values" not in value


@pytest.mark.parametrize(
    "changes",
    [
        {"records": (True,)},
        {"records": (0,)},
        {"records": tuple(range(1, 5002))},
        {"object_id": "platform.secret"},
        {"action_id": "unknown.action"},
        {"read": (("password", "plain"),)},
        {"read": (("role_name", "masked"),)},
        {"query": (("role_name", "eq", "management.read", "filter"),)},
    ],
)
def test_domain_rejects_unknown_sources_masks_operations_and_budgets(changes):
    with pytest.raises(BusinessException):
        domain(**changes)


def test_union_preserves_purpose_and_original_record_scope_without_truncation():

    one, two = domain(records=(201,)), domain(records=(202,))
    proof = with_sources(receipt(), (one, two))
    assert proof["source_domains"] == [domain()]
    other = domain(query=())
    assert len(with_sources(proof, (other,))["source_domains"]) == 2


def test_business_payload_and_caller_grants_cannot_enter_a_source_domain():

    for extra in (
        {"values": {"role_name": "SECRET"}},
        {"grants": ["allow"]},
        {"tenant_id": "72"},
    ):
        with pytest.raises(BusinessException):
            with_sources(receipt(), (dict(domain(), **extra),))
