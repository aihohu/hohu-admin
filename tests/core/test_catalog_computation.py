"""Derived identities do not reconstruct unrelated catalog documents."""

from dataclasses import replace

import pytest

from app.core.exceptions import BusinessException
from app.core.field_policy import Catalog, ObjectSpec, documents
from app.core.field_policy.contracts import primitive
from app.modules.system.field_policy.ordinary_catalog import production_catalog


def test_catalog_parts_only_serializes_its_identity_inputs(monkeypatch):
    original = production_catalog()
    catalog = Catalog(
        tuple(original.objects.values()), tuple(original.actions.values())
    )
    expected = (
        documents.digest(documents.catalog_document(catalog)["actions"]),
        documents.digest(
            [
                {
                    "object_id": obj.object_id,
                    "scope_adapter": obj.scope_adapter,
                    "sources": {f.field_id: primitive(f.sources) for f in obj.fields},
                }
                for obj in sorted(catalog.objects.values(), key=lambda o: o.object_id)
            ]
        ),
    )
    seen = []

    def observed(value):
        seen.append(type(value))
        return primitive(value)

    monkeypatch.setattr(documents, "primitive", observed)
    assert documents.catalog_parts(catalog) == expected
    assert ObjectSpec not in seen, "Object documents are not inputs to either digest"


def test_catalog_parts_reuses_only_the_same_immutable_instances_digests(monkeypatch):
    original = production_catalog()
    catalog = Catalog(
        tuple(original.objects.values()), tuple(original.actions.values())
    )
    calls = []
    digest = documents.digest

    def observed(value, *args):
        calls.append(1)
        return digest(value, *args)

    monkeypatch.setattr(documents, "digest", observed)
    first = documents.catalog_parts(catalog)
    assert documents.catalog_parts(catalog) == first
    assert len(calls) == 2, "An immutable instance computes its two identities once"
    changed = Catalog(
        tuple(
            replace(obj, scope_adapter=obj.scope_adapter + ".next")
            for obj in catalog.objects.values()
        ),
        tuple(catalog.actions.values()),
    )
    assert documents.catalog_parts(changed)[1] != first[1]
    assert len(calls) == 4
    document = documents.catalog_document(catalog)
    document["objects"][0]["scope_adapter"] += ".changed"
    assert documents.catalog_parts(catalog) == first
    with pytest.raises(BusinessException):
        documents.load_catalog(document, expected_digest=catalog.digest)
