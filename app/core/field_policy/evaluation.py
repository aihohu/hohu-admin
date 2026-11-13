"""Pure per-record authorization and bounded typed projection."""

import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

from app.utils.mask_util import MaskUtil

from .contracts import (
    LEVELS,
    READS,
    WRITES,
    Access,
    Catalog,
    FieldRef,
    FieldUse,
    RecordContext,
    canonical,
    denied,
    identity,
    invalid,
    limit,
    primitive,
    require,
    sequence,
    tenant,
)


@dataclass(frozen=True)
class Grant:
    tenant_id: int
    role_id: str
    ref: FieldRef
    access: Access
    effect: str = "allow"
    level: str = "plain"
    mask_id: str | None = None


@dataclass(frozen=True)
class CompiledRules:
    catalog_digest: str
    index: Mapping

    def __post_init__(self):
        object.__setattr__(self, "index", MappingProxyType(dict(self.index)))


def compile_rules(catalog: Catalog, grants: tuple[Grant, ...]) -> CompiledRules:
    require(type(catalog) is Catalog)
    sequence(grants, 262144, Grant)
    groups, index = defaultdict(lambda: [0, 2]), {}
    total = 0
    for grant in grants:
        tenant(grant.tenant_id)
        identity(grant.role_id)
        require(type(grant.ref) is FieldRef and type(grant.access) is Access)
        require(grant.ref in catalog.fields)
        field = catalog.fields[grant.ref]
        obj = catalog.objects[grant.ref.object_id]
        require(obj.domain_kind == "tenant" and grant.access in field.accesses)
        require(type(grant.effect) is str and grant.effect in {"allow", "deny"})
        require(type(grant.level) is str and grant.level in {"plain", "masked"})
        if grant.level == "masked":
            require(
                grant.access.operation in READS
                and field.mask_id is not None
                and grant.mask_id in (None, field.mask_id)
            )
        else:
            require(grant.mask_id is None)
        key = (grant.tenant_id, grant.role_id, grant.ref, grant.access)
        require(key not in index)
        index[key] = grant
        group = groups[grant.tenant_id, grant.role_id, grant.ref.object_id]
        size = len(canonical(primitive(grant), obj.max_rule_bytes)) + (
            1 if group[0] else 0
        )
        total += size + (2 if group[0] == 0 else 0)
        group[0] += 1
        group[1] += size
        limit(group[0] <= obj.max_rules and group[1] <= obj.max_rule_bytes)
        limit(total <= 64 * 1024 * 1024)
    return CompiledRules(catalog.digest, MappingProxyType(index))


def action_context(catalog, rules, context, action_id):
    require(
        type(catalog) is Catalog
        and type(rules) is CompiledRules
        and type(context) is RecordContext
    )
    require(
        rules.catalog_digest == catalog.digest
        and type(action_id) is str
        and action_id in catalog.actions
    )
    action = catalog.actions[action_id]
    require(
        context.object_id in action.object_ids
        and catalog.objects[context.object_id].domain_kind == "tenant"
    )
    if not set(action.permission_codes) <= context.permission_codes:
        raise denied()
    return action


@dataclass(frozen=True)
class Decision:
    allowed: bool = False
    level: str = "hidden"
    mask_id: str | None = None


def _decision(catalog, rules, context, action, ref, access):
    field = catalog.fields[ref]
    if FieldUse(ref, access) not in action.uses:
        return Decision()
    if field.structural and access.operation in READS:
        level = "plain"
    else:
        matched = [
            g
            for role in context.role_ids
            if (g := rules.index.get((context.tenant_id, role, ref, access)))
            is not None
        ]
        if not matched or any(g.effect == "deny" for g in matched):
            return Decision()
        level = max((g.level for g in matched), key=LEVELS.__getitem__)
    if access.operation in READS:
        level = min(
            (level, field.read_cap, dict(action.read_caps).get(ref, "plain")),
            key=LEVELS.__getitem__,
        )
        if access.operation == "export":
            source = _decision(
                catalog, rules, context, action, ref, Access("read", "management.read")
            )
            level = min((level, source.level), key=LEVELS.__getitem__)
    return Decision(
        level != "hidden", level, field.mask_id if level == "masked" else None
    )


def resolve_fields(catalog, rules, context, action_id, access):
    action = action_context(catalog, rules, context, action_id)
    require(type(access) is Access)
    require(
        any(
            u.ref.object_id == context.object_id and u.access == access
            for u in action.uses
        )
    )
    return MappingProxyType(
        {
            f.field_id: _decision(
                catalog,
                rules,
                context,
                action,
                FieldRef(context.object_id, f.field_id),
                access,
            )
            for f in catalog.objects[context.object_id].fields
        }
    )


def normalize_paths(catalog, object_id, paths):
    require(
        type(catalog) is Catalog
        and type(object_id) is str
        and object_id in catalog.objects
    )
    sequence(paths, 256, str)
    result = []
    for path in paths:
        require((object_id, path) in catalog.aliases)
        result.append(catalog.aliases[object_id, path])
    require(len(set(result)) == len(result))
    return tuple(result)


def validate_write(catalog, rules, context, action_id, access, supplied_paths):
    require(type(access) is Access and access.operation in WRITES)
    normalized = normalize_paths(catalog, context.object_id, supplied_paths)
    decisions = resolve_fields(catalog, rules, context, action_id, access)
    if any(not decisions[path].allowed for path in normalized):
        raise denied()
    return normalized


class _PayloadBudget:
    def __init__(self):
        self.nodes = 0
        self.bytes = 0

    def freeze(self, value, depth=0, *, json_only=False):
        self.nodes += 1
        limit(self.nodes <= 16384 and depth <= 8)
        if value is None or type(value) in (bool, int):
            if type(value) is int:
                limit(value.bit_length() <= 64)
            self.bytes += 24
            result = value
        elif type(value) is float:
            require(math.isfinite(value))
            self.bytes += 24
            result = value
        elif type(value) is str:
            limit(len(value) <= 1024 * 1024)
            try:
                self.bytes += len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            except UnicodeError:
                raise invalid() from None
            result = value
        elif type(value) in (date, datetime):
            require(not json_only)
            self.bytes += 48
            result = value
        elif type(value) in (dict, MappingProxyType):
            limit(len(value) <= 16384)
            result = {}
            for key, item in value.items():
                require(type(key) is str)
                self.freeze(key, depth + 1, json_only=json_only)
                result[key] = self.freeze(item, depth + 1, json_only=json_only)
            self.bytes += 2 + 2 * len(value)
            result = MappingProxyType(result)
        elif type(value) in (tuple, list):
            limit(len(value) <= 16384)
            result = tuple(
                self.freeze(item, depth + 1, json_only=json_only) for item in value
            )
            self.bytes += 2 + len(value)
        else:
            require(False)
        limit(self.bytes <= 1024 * 1024)
        return result


def _typed(field, value):
    if value is None:
        require(field.nullable)
        return None
    kind = field.value_type
    if kind == "id":
        if type(value) is int:
            value = str(value)
        identity(value)
    elif kind == "opaque_id":
        require(
            type(value) is str
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value) is not None
        )
    elif kind == "string":
        require(type(value) is str)
    elif kind == "integer":
        require(type(value) is int)
    elif kind == "number":
        require(type(value) in (int, float))
    elif kind == "boolean":
        require(type(value) is bool)
    elif kind == "date":
        require(type(value) is date)
    elif kind == "datetime":
        require(type(value) is datetime)
    return value


def _mask(value, mask_id):
    if mask_id == "phone/v1":
        return (
            MaskUtil.phone(value)
            if len(value) == 11 and value.isascii() and value.isdigit()
            else "***"
        )
    local, sep, domain = value.partition("@")
    return (
        MaskUtil.email(value)
        if sep and len(local) >= 2 and domain and "@" not in domain
        else "***"
    )


def project(catalog, rules, context, action_id, access, values):
    require(type(access) is Access and access.operation in READS)
    require(type(values) in (dict, MappingProxyType))
    limit(len(values) <= 256)
    sequence(tuple(values), 256, str)
    require(all(FieldRef(context.object_id, key) in catalog.fields for key in values))
    decisions = resolve_fields(catalog, rules, context, action_id, access)
    budget, result = _PayloadBudget(), {}
    for key, value in values.items():
        budget.freeze(key)
        frozen = budget.freeze(
            value,
            json_only=catalog.fields[FieldRef(context.object_id, key)].value_type
            == "json",
        )
        decision = decisions[key]
        if decision.allowed:
            typed = _typed(catalog.fields[FieldRef(context.object_id, key)], frozen)
            result[key] = (
                _mask(typed, decision.mask_id)
                if typed is not None and decision.level == "masked"
                else typed
            )
    return MappingProxyType(result)
