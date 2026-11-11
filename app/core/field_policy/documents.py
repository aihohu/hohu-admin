"""Versioned persistence codecs; documents are data, never authorization proof."""

import hashlib
import json
from dataclasses import fields

from app.core.field_policy.contracts import (
    Access,
    ActionSpec,
    Catalog,
    FieldRef,
    FieldSpec,
    FieldUse,
    ObjectSpec,
    canonical,
    identity,
    limit,
    primitive,
    require,
    tenant,
)
from app.core.field_policy.evaluation import Grant, compile_rules


def read_json(encoded):
    """Reject ambiguous keys, non-JSON constants and excessive nesting."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    def invalid(_value):
        require(False)

    try:
        return json.loads(encoded, object_pairs_hook=unique, parse_constant=invalid)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        require(False)


def digest(document, budget=1024 * 1024):
    return hashlib.sha256(canonical(document, budget)).hexdigest()


def _keys(value, names):
    require(type(value) is dict and set(value) == set(names))
    return value


def _record(value, cls):
    return _keys(value, (field.name for field in fields(cls)))


def _list(value, maximum):
    require(type(value) is list)
    limit(len(value) <= maximum)
    return value


def catalog_document(catalog):
    require(type(catalog) is Catalog)
    return {
        "schema_version": 2,
        "objects": [
            primitive(catalog.objects[name]) for name in sorted(catalog.objects)
        ],
        "actions": [
            primitive(catalog.actions[name]) for name in sorted(catalog.actions)
        ],
    }


def load_catalog(document, *, expected_digest):
    _keys(document, ("schema_version", "objects", "actions"))
    require(type(document["schema_version"]) is int and document["schema_version"] == 2)
    objects, actions = [], []
    for obj in _list(document["objects"], 64):
        _record(obj, ObjectSpec)
        specs = []
        for spec in _list(obj["fields"], 256):
            _record(spec, FieldSpec)
            accesses = tuple(
                Access(**_record(a, Access)) for a in _list(spec["accesses"], 64)
            )
            sources = tuple(
                FieldRef(**_record(r, FieldRef)) for r in _list(spec["sources"], 256)
            )
            specs.append(
                FieldSpec(
                    **(
                        spec
                        | {
                            "accesses": accesses,
                            "sources": sources,
                            "aliases": tuple(_list(spec["aliases"], 16)),
                        }
                    )
                )
            )
        objects.append(ObjectSpec(**(obj | {"fields": tuple(specs)})))
    for action in _list(document["actions"], 512):
        _record(action, ActionSpec)
        uses = []
        for use in _list(action["uses"], 256):
            _record(use, FieldUse)
            uses.append(
                FieldUse(
                    FieldRef(**_record(use["ref"], FieldRef)),
                    Access(**_record(use["access"], Access)),
                )
            )
        caps = []
        for cap in _list(action["read_caps"], 256):
            require(type(cap) is list and len(cap) == 2)
            caps.append((FieldRef(**_record(cap[0], FieldRef)), cap[1]))
        actions.append(
            ActionSpec(
                **(
                    action
                    | {
                        "object_ids": tuple(_list(action["object_ids"], 64)),
                        "permission_codes": tuple(
                            _list(action["permission_codes"], 64)
                        ),
                        "uses": tuple(uses),
                        "read_caps": tuple(caps),
                    }
                )
            )
        )
    catalog = Catalog(tuple(objects), tuple(actions))
    require(catalog.digest == expected_digest)
    return catalog


def catalog_parts(catalog):
    """Separate action/adapter identities also belong to a sealed manifest."""
    document = catalog_document(catalog)
    return (
        digest(document["actions"]),
        digest(
            [
                {
                    "object_id": obj.object_id,
                    "scope_adapter": obj.scope_adapter,
                    "sources": {f.field_id: primitive(f.sources) for f in obj.fields},
                }
                for obj in sorted(
                    catalog.objects.values(), key=lambda obj: obj.object_id
                )
            ]
        ),
    )


def _scope(catalog, tenant_id, role_id, object_id):
    require(type(catalog) is Catalog)
    tenant(tenant_id)
    require(type(role_id) is int)
    identity(str(role_id))
    require(type(object_id) is str and object_id in catalog.objects)
    require(catalog.objects[object_id].domain_kind == "tenant")


def rules_document(catalog, grants, *, tenant_id, role_id, object_id):
    _scope(catalog, tenant_id, role_id, object_id)
    compile_rules(catalog, grants)
    require(
        all(
            (g.tenant_id, g.role_id, g.ref.object_id)
            == (tenant_id, str(role_id), object_id)
            for g in grants
        )
    )
    result = [
        {
            "field_id": g.ref.field_id,
            "access": primitive(g.access),
            "effect": g.effect,
            "level": g.level,
            "mask_id": g.mask_id,
        }
        for g in grants
    ]
    result.sort(
        key=lambda r: (
            r["field_id"],
            r["access"]["purpose"],
            r["access"]["operation"],
            r["access"]["use"],
        )
    )
    canonical(result, catalog.objects[object_id].max_rule_bytes)
    return result


def load_rules(catalog, document, *, tenant_id, role_id, object_id):
    _scope(catalog, tenant_id, role_id, object_id)
    grants = []
    for rule in _list(document, catalog.objects[object_id].max_rules):
        _keys(rule, ("field_id", "access", "effect", "level", "mask_id"))
        grants.append(
            Grant(
                tenant_id,
                str(role_id),
                FieldRef(object_id, rule["field_id"]),
                Access(**_record(rule["access"], Access)),
                rule["effect"],
                rule["level"],
                rule["mask_id"],
            )
        )
    grants = tuple(grants)
    normalized = rules_document(
        catalog, grants, tenant_id=tenant_id, role_id=role_id, object_id=object_id
    )
    require(normalized == document)
    return grants
