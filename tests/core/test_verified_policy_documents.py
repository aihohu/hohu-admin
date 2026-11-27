import json

import pytest

from app.core.exceptions import BusinessException
from app.core.field_policy.documents import catalog_document, digest
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.service.verified_policy_documents import documents_for


def test_warm_documents_recheck_actual_contents_and_scope():
    catalog = production_catalog()
    cache = documents_for(catalog)
    document = catalog_document(catalog)
    text = json.dumps(document)
    assert cache.catalog_parts(text) == cache.catalog_parts(text)
    with pytest.raises(BusinessException):
        cache.catalog_parts(json.dumps(document | {"schema_version": 7}))
    rules = [
        {
            "field_id": "nickname",
            "access": {"operation": "read", "purpose": "management.read", "use": ""},
            "effect": "allow",
            "level": "plain",
            "mask_id": None,
        }
    ]
    text = json.dumps(rules)
    first = cache.rules(0, 123, "system.user", digest(rules), text)
    assert cache.rules(0, 123, "system.user", digest(rules), text) is first
    assert cache.rules(71, 123, "system.user", digest(rules), text)[0].tenant_id == 71
    assert cache.rules(0, 124, "system.user", digest(rules), text)[0].role_id == "124"
    with pytest.raises(BusinessException):
        cache.rules(0, 123, "system.user", digest(rules), "[]")
