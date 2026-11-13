"""Bounded, immutable server declarations. These values are not auth proofs."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from types import MappingProxyType

from app.core.exceptions import AuthorizationException, InvalidParameterException

LEVELS = {"hidden": 0, "masked": 1, "plain": 2}
DOMAINS = frozenset({"tenant", "platform", "system", "public"})
READS = frozenset({"read", "export"})
WRITES = frozenset({"create", "update", "replace"})


def invalid():
    return InvalidParameterException(
        "字段授权契约无效", error_code="FIELD_POLICY_INVALID"
    )


def denied():
    return AuthorizationException("字段权限不足", error_code="FIELD_POLICY_DENIED")


def limit(condition):
    if not condition:
        raise InvalidParameterException(
            "授权计划超出预算", error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
        )


def require(condition):
    if not condition:
        raise invalid()


def name(value):
    require(
        type(value) is str
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:/-]{0,127}", value) is not None
    )


def tenant(value):
    require(type(value) is int and 0 <= value < 2**63)


def identity(value):
    require(
        type(value) is str
        and re.fullmatch(r"[1-9][0-9]{0,18}", value) is not None
        and int(value) < 2**63
    )


def sequence(value, size, cls=None):
    require(type(value) is tuple)
    limit(len(value) <= size)
    if cls is not None:
        require(all(type(item) is cls for item in value))


def canonical(value, budget):
    """Only called with internally constructed JSON primitives; bounded encoding."""
    chunks, size = [], 0
    for part in json.JSONEncoder(
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).iterencode(value):
        try:
            encoded = part.encode("utf-8")
        except UnicodeError:
            raise invalid() from None
        size += len(encoded)
        limit(size <= budget)
        chunks.append(encoded)
    return b"".join(chunks)


def primitive(value):
    if is_dataclass(value):
        return {
            field.name: primitive(getattr(value, field.name)) for field in fields(value)
        }
    if type(value) is tuple:
        return [primitive(item) for item in value]
    return value


@dataclass(frozen=True, order=True)
class Access:
    operation: str
    purpose: str
    use: str = ""

    def __post_init__(self):
        require(all(type(v) is str for v in (self.operation, self.purpose, self.use)))
        ordinary = {
            "management.read": {"read"},
            "own-account": {"read", "update"},
            "management.create": {"create"},
            "management.update": {"update"},
            "management.relations": {"replace"},
            "export": {"export"},
            "import": {"create", "update", "replace"},
        }
        uses = {
            "exact": {"filter", "join"},
            "contains": {"filter"},
            "eq": {"filter", "join"},
            "sort": {"sort"},
            "group": {"group"},
            "distinct": {"distinct"},
            "aggregate": {"aggregate"},
        }
        require(
            (self.use == "" and self.operation in ordinary.get(self.purpose, set()))
            or (
                self.purpose == "management.query"
                and self.use in uses.get(self.operation, set())
            )
            or (self.operation, self.purpose, self.use) == ("exact", "import", "match")
        )


@dataclass(frozen=True, order=True)
class FieldRef:
    object_id: str
    field_id: str

    def __post_init__(self):
        name(self.object_id)
        name(self.field_id)


@dataclass(frozen=True)
class FieldSpec:
    field_id: str
    value_type: str
    accesses: tuple[Access, ...]
    nullable: bool = False
    aliases: tuple[str, ...] = ()
    mask_id: str | None = None
    read_cap: str = "plain"
    structural: bool = False
    sources: tuple[FieldRef, ...] = ()


@dataclass(frozen=True)
class ObjectSpec:
    object_id: str
    domain_kind: str
    scope_adapter: str
    fields: tuple[FieldSpec, ...]
    max_rules: int = 2048
    max_rule_bytes: int = 262144


@dataclass(frozen=True, order=True)
class FieldUse:
    ref: FieldRef
    access: Access

    def __post_init__(self):
        require(type(self.ref) is FieldRef and type(self.access) is Access)


@dataclass(frozen=True)
class ActionSpec:
    action_id: str
    object_ids: tuple[str, ...]
    uses: tuple[FieldUse, ...]
    permission_codes: tuple[str, ...]
    read_caps: tuple[tuple[FieldRef, str], ...] = ()
    kind: str = "data"


@dataclass(frozen=True, init=False)
class Catalog:
    objects: Mapping[str, ObjectSpec]
    actions: Mapping[str, ActionSpec]
    fields: Mapping[FieldRef, FieldSpec]
    aliases: Mapping[tuple[str, str], str]
    digest: str

    def __init__(
        self, objects: tuple[ObjectSpec, ...], actions: tuple[ActionSpec, ...]
    ):
        sequence(objects, 64, ObjectSpec)
        sequence(actions, 512, ActionSpec)
        object_map, action_map, field_map, aliases = {}, {}, {}, {}
        edges = 0
        for obj in objects:
            name(obj.object_id)
            name(obj.scope_adapter)
            require(
                type(obj.domain_kind) is str
                and obj.domain_kind in DOMAINS
                and obj.object_id not in object_map
            )
            require(type(obj.max_rules) is int and 1 <= obj.max_rules <= 2048)
            require(
                type(obj.max_rule_bytes) is int and 1 <= obj.max_rule_bytes <= 262144
            )
            sequence(obj.fields, 256, FieldSpec)
            normalized = []
            for field in obj.fields:
                name(field.field_id)
                require(
                    type(field.value_type) is str
                    and field.value_type
                    in {
                        "string",
                        "id",
                        "integer",
                        "number",
                        "boolean",
                        "date",
                        "datetime",
                        "json",
                    }
                )
                require(type(field.nullable) is bool and type(field.structural) is bool)
                require(type(field.read_cap) is str and field.read_cap in LEVELS)
                require(
                    field.mask_id is None
                    or (
                        type(field.mask_id) is str
                        and field.mask_id in {"phone/v1", "email/v1"}
                    )
                )
                require(field.mask_id is None or field.value_type == "string")
                require(field.read_cap != "masked" or field.mask_id is not None)
                sequence(field.accesses, 64, Access)
                sequence(field.aliases, 16, str)
                sequence(field.sources, 256, FieldRef)
                require(
                    len(set(field.accesses)) == len(field.accesses)
                    and len(set(field.sources)) == len(field.sources)
                )
                if field.structural:
                    require(
                        field.value_type == "id"
                        and all(a.operation in READS for a in field.accesses)
                        and field.read_cap == "plain"
                        and not field.nullable
                    )
                ref = FieldRef(obj.object_id, field.field_id)
                require(ref not in field_map)
                for alias in (field.field_id, *field.aliases):
                    name(alias)
                    require((obj.object_id, alias) not in aliases)
                    aliases[obj.object_id, alias] = field.field_id
                normalized_field = FieldSpec(
                    field.field_id,
                    field.value_type,
                    tuple(sorted(field.accesses)),
                    field.nullable,
                    tuple(sorted(field.aliases)),
                    field.mask_id,
                    field.read_cap,
                    field.structural,
                    tuple(sorted(field.sources)),
                )
                field_map[ref] = normalized_field
                normalized.append(normalized_field)
                edges += len(field.sources)
                limit(edges <= 4096)
            object_map[obj.object_id] = ObjectSpec(
                obj.object_id,
                obj.domain_kind,
                obj.scope_adapter,
                tuple(sorted(normalized, key=lambda f: f.field_id)),
                obj.max_rules,
                obj.max_rule_bytes,
            )
        visiting, depths = set(), {}

        def visit(ref):
            require(ref in field_map and ref not in visiting)
            if ref in depths:
                return depths[ref]
            visiting.add(ref)
            limit(len(visiting) <= 16)
            depth = 1 + max((visit(dep) for dep in field_map[ref].sources), default=0)
            limit(depth <= 16)
            visiting.remove(ref)
            depths[ref] = depth
            return depth

        for ref in field_map:
            visit(ref)
        for action in actions:
            name(action.action_id)
            require(
                action.action_id not in action_map
                and type(action.kind) is str
                and action.kind in {"data", "special", "navigation"}
            )
            sequence(action.object_ids, 64, str)
            sequence(action.permission_codes, 64, str)
            sequence(action.uses, 256, FieldUse)
            sequence(action.read_caps, 256, tuple)
            require(
                len(set(action.object_ids)) == len(action.object_ids)
                and len(set(action.permission_codes)) == len(action.permission_codes)
                and len(set(action.uses)) == len(action.uses)
            )
            require(all(obj in object_map for obj in action.object_ids))
            for code in action.permission_codes:
                require(
                    re.fullmatch(
                        r"[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*", code
                    )
                    is not None
                )
            if action.kind == "navigation":
                require(
                    not action.object_ids and not action.uses and not action.read_caps
                )
            else:
                require(bool(action.object_ids) and bool(action.permission_codes))
                require(action.kind != "data" or bool(action.uses))
            use_set = set(action.uses)
            for use in action.uses:
                require(
                    use.ref in field_map
                    and use.ref.object_id in action.object_ids
                    and use.access in field_map[use.ref].accesses
                )
                if use.access.operation == "export":
                    require(
                        FieldUse(use.ref, Access("read", "management.read")) in use_set
                    )
            cap_refs = set()
            for cap in action.read_caps:
                require(
                    len(cap) == 2 and type(cap[0]) is FieldRef and type(cap[1]) is str
                )
                ref, level = cap
                require(ref in field_map and ref not in cap_refs and level in LEVELS)
                require(
                    any(
                        u.ref == ref and u.access.operation in READS
                        for u in action.uses
                    )
                )
                require(level != "masked" or field_map[ref].mask_id is not None)
                cap_refs.add(ref)
            action_map[action.action_id] = ActionSpec(
                action.action_id,
                tuple(sorted(action.object_ids)),
                tuple(sorted(action.uses)),
                tuple(sorted(action.permission_codes)),
                tuple(sorted(action.read_caps)),
                action.kind,
            )
        encoded = canonical(
            {
                "objects": [primitive(object_map[k]) for k in sorted(object_map)],
                "actions": [primitive(action_map[k]) for k in sorted(action_map)],
            },
            1024 * 1024,
        )
        for key, value in (
            ("objects", object_map),
            ("actions", action_map),
            ("fields", field_map),
            ("aliases", aliases),
        ):
            object.__setattr__(self, key, MappingProxyType(value))
        object.__setattr__(self, "digest", hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True)
class RecordContext:
    tenant_id: int
    object_id: str
    record_key: str
    role_ids: frozenset[str]
    permission_codes: frozenset[str]

    def __post_init__(self):
        tenant(self.tenant_id)
        name(self.object_id)
        require(type(self.record_key) is str and 0 < len(self.record_key) <= 128)
        require(
            type(self.role_ids) is frozenset
            and type(self.permission_codes) is frozenset
        )
        limit(len(self.role_ids) <= 4096 and len(self.permission_codes) <= 4096)
        for role in self.role_ids:
            identity(role)
        for code in self.permission_codes:
            name(code)
