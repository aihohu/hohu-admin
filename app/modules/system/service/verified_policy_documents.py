"""Reuse decoding of immutable JSONB contents, never role membership or admission."""

import json
from functools import lru_cache

from app.core.exceptions import BusinessException
from app.core.field_policy.contracts import primitive
from app.core.field_policy.documents import (
    catalog_parts,
    digest,
    load_catalog,
    load_rules,
)


class VerifiedPolicyDocuments:
    def __init__(self, catalog):
        self.catalog = catalog
        self.objects = {
            name: (obj.domain_kind, digest(primitive(obj)), obj.scope_adapter)
            for name, obj in catalog.objects.items()
        }

        @lru_cache(maxsize=1)
        def parts(text):
            return catalog_parts(
                load_catalog(json.loads(text), expected_digest=catalog.digest)
            )

        def parse_rules(tenant, role, obj, expected, text):
            document = json.loads(text)
            if digest(document) != expected:
                raise BusinessException(
                    503, "通用字段策略未就绪", error_code="FIELD_POLICY_UNAVAILABLE"
                )
            return load_rules(
                catalog, document, tenant_id=tenant, role_id=role, object_id=obj
            )

        self.catalog_parts = parts
        self._parse_rules = parse_rules
        self._small_rules = lru_cache(maxsize=128)(parse_rules)

    def rules(self, tenant, role, obj, expected, text):
        parse = self._small_rules if len(text) <= 32768 else self._parse_rules
        return parse(tenant, role, obj, expected, text)


_current = None


def documents_for(catalog):
    global _current
    current = _current
    if current is None or current.catalog is not catalog:
        current = _current = VerifiedPolicyDocuments(catalog)
    return current
