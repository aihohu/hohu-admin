"""Closed, digest-bound persistence documents for the generic field kernel."""

from copy import deepcopy

import pytest

from app.core.exceptions import BusinessException
from app.core.field_policy import (
    Access,
    Catalog,
    FieldRef,
    FieldSpec,
    Grant,
    ObjectSpec,
)
from app.core.field_policy.documents import (
    catalog_document,
    load_catalog,
    load_rules,
    rules_document,
)


def catalog():
    return Catalog(
        (
            ObjectSpec(
                "sample.item",
                "tenant",
                "sample.scope",
                (FieldSpec("name", "string", (Access("read", "management.read"),)),),
            ),
        ),
        (),
    )


def test_catalog_roundtrip_and_recomputed_digest():
    original = catalog()
    document = catalog_document(original)
    assert (
        load_catalog(document, expected_digest=original.digest).digest
        == original.digest
    )
    corrupt = deepcopy(document)
    corrupt["objects"][0]["fields"][0]["nullable"] = True
    with pytest.raises(BusinessException):
        load_catalog(corrupt, expected_digest=original.digest)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update(schema_version=1),
        lambda doc: doc.update(extra=True),
        lambda doc: doc["objects"][0].update(extra=True),
        lambda doc: doc["objects"][0]["fields"][0].update(extra=True),
    ],
)
def test_catalog_rejects_old_and_unknown_document_keys(mutate):
    original = catalog()
    document = catalog_document(original)
    mutate(document)
    with pytest.raises(BusinessException):
        load_catalog(document, expected_digest=original.digest)


def test_rules_roundtrip_is_scoped_and_deny_remains_explicit():
    original = catalog()
    grant = Grant(
        0,
        "201",
        FieldRef("sample.item", "name"),
        Access("read", "management.read"),
        "deny",
    )
    document = rules_document(
        original, (grant,), tenant_id=0, role_id=201, object_id="sample.item"
    )
    assert load_rules(
        original, document, tenant_id=0, role_id=201, object_id="sample.item"
    ) == (grant,)
    with pytest.raises(BusinessException):
        rules_document(
            original, (grant,), tenant_id=1, role_id=201, object_id="sample.item"
        )
    bad = deepcopy(document)
    bad[0]["extra"] = True
    with pytest.raises(BusinessException):
        load_rules(original, bad, tenant_id=0, role_id=201, object_id="sample.item")


def test_empty_rules_are_explicitly_empty():
    assert (
        load_rules(catalog(), [], tenant_id=0, role_id=201, object_id="sample.item")
        == ()
    )
