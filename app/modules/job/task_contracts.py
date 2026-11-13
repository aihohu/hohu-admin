"""Dormant task schemas and call shapes, not execution or identity authority.

Only a fresh production owner may claim work. These declarations do not inspect
creator roles, create TenantContext, schedule tasks, or offer an execute method.
"""

import hashlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import get_args

from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.exceptions import BusinessException, BusinessRuleException
from app.modules.job.task_registry import TaskScope
from app.utils.closed_json import closed_object

_PROTOCOLS = {
    TaskScope.TENANT: "tenant.no-data/v1",
    TaskScope.PLATFORM: "platform.transfer-retention/v1",
}
_SUBJECTS = frozenset(
    {
        "tenant",
        "tenantid",
        "tenantcontext",
        "tid",
        "user",
        "userid",
        "uid",
        "actor",
        "actorid",
        "actoruserid",
        "role",
        "roles",
        "roleid",
        "roleids",
        "principal",
        "principalid",
        "identity",
        "context",
        "purpose",
        "permission",
        "permissions",
        "permissioncodes",
        "grant",
        "grants",
        "authorization",
        "authversion",
        "serviceprotocol",
        "sub",
        "pver",
    }
)


def _schema_error():
    return BusinessRuleException(
        "Task schema and service protocol required",
        error_code="JOB_ARGUMENT_SCHEMA_REQUIRED",
    )


def _argument_error():
    return BusinessRuleException(
        "Task arguments rejected", error_code="JOB_ARGUMENT_INVALID"
    )


class ClosedTaskArgs(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_default=True,
        populate_by_name=True,
    )


def _subject(key):
    normalized = "".join(c for c in key.lower() if c.isalnum())
    return normalized in _SUBJECTS or normalized.endswith(
        ("tenantid", "tenantids", "roleids", "principalid", "actoruserid")
    )


def _schema(model):
    seen = set()

    def check_model(candidate):
        if (
            not isinstance(candidate, type)
            or not issubclass(candidate, ClosedTaskArgs)
            or any(
                candidate.model_config.get(key) != expected
                for key, expected in (
                    ("extra", "forbid"),
                    ("strict", True),
                    ("frozen", True),
                    ("validate_default", True),
                    ("populate_by_name", True),
                )
            )
        ):
            raise _schema_error()
        if candidate in seen:
            return
        if len(seen) >= 256:
            raise _schema_error()
        seen.add(candidate)
        for key, definition in candidate.model_fields.items():
            for name in (
                key,
                definition.alias,
                definition.validation_alias,
                definition.serialization_alias,
            ):
                if name is not None and (type(name) is not str or _subject(name)):
                    raise _schema_error()
            check_annotation(definition.annotation)

    def check_annotation(annotation):
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            check_model(annotation)
        else:
            for child in get_args(annotation):
                check_annotation(child)

    check_model(model)
    try:
        schema, encoded = closed_object(
            model.model_json_schema(), max_bytes=32768, max_nodes=4096, max_depth=16
        )
    except (BusinessException, ValueError, TypeError, RecursionError):
        raise _schema_error() from None
    definitions = schema.get("$defs", {})

    def visit(node, active=()):
        if type(node) is not dict or len(active) > 16:
            raise _schema_error()
        if "$ref" in node:
            target = node["$ref"]
            if (
                not isinstance(target, str)
                or not target.startswith("#/$defs/")
                or target in active
            ):
                raise _schema_error()
            definition = definitions.get(target.removeprefix("#/$defs/"))
            visit(definition, (*active, target))
            return
        if node.get("type") == "object":
            if node.get("additionalProperties") is not False:
                raise _schema_error()
            for key, child in node.get("properties", {}).items():
                if _subject(key):
                    raise _schema_error()
                visit(child, active)
        elif node.get("type") == "array":
            visit(node.get("items"), active)
        elif any(key in node for key in ("anyOf", "oneOf", "allOf")):
            for key in ("anyOf", "oneOf", "allOf"):
                for child in node.get(key, ()):
                    visit(child, active)
        elif (
            node.get("type") not in {"string", "integer", "number", "boolean", "null"}
            and "enum" not in node
            and "const" not in node
        ):
            raise _schema_error()

    visit(schema)
    return schema, encoded


def _public_schema(value):
    if isinstance(value, dict):
        return {
            key: (
                {name: _public_schema(node) for name, node in child.items()}
                if key in {"properties", "$defs"}
                else _public_schema(child)
            )
            for key, child in value.items()
            if key not in {"default", "description", "examples", "title"}
        }
    if isinstance(value, list):
        return [_public_schema(child) for child in value]
    return value


@dataclass(frozen=True, slots=True)
class TaskSpec:
    key: str
    label_key: str
    scope: TaskScope
    args_model: type[ClosedTaskArgs] = field(repr=False)
    function: Callable = field(repr=False)
    service_protocol: str
    timeout_seconds: int = 30
    max_retries: int = 0
    _schema_bytes: bytes = field(init=False, repr=False)

    def __post_init__(self):
        if (
            type(self.key) is not str
            or not 1 <= len(self.key) <= 64
            or not all(c.isascii() and (c.isalnum() or c == "_") for c in self.key)
            or type(self.label_key) is not str
            or not 1 <= len(self.label_key) <= 128
            or type(self.scope) is not TaskScope
            or self.service_protocol != _PROTOCOLS[self.scope]
            or type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 60
            or type(self.max_retries) is not int
            or not 0 <= self.max_retries <= 3
        ):
            raise _schema_error()
        if not inspect.isfunction(self.function) or not inspect.iscoroutinefunction(
            self.function
        ):
            raise _schema_error()
        parameters = tuple(inspect.signature(self.function).parameters.values())
        if len(parameters) != 1 or parameters[0].kind not in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            raise _schema_error()
        _, encoded = _schema(self.args_model)
        object.__setattr__(self, "_schema_bytes", encoded)

    def _check_schema(self):
        schema, encoded = _schema(self.args_model)
        if encoded != self._schema_bytes:
            raise _schema_error()
        return schema

    def public_schema(self):
        return _public_schema(self._check_schema())

    def call_arguments(self, prepared):
        """Return one detached positional dict; this does not authorize calling it."""
        if (
            type(prepared) is not PreparedTaskArgs
            or prepared._spec is not self
            or type(prepared._canonical) is not bytes
        ):
            raise _argument_error()
        try:
            raw = prepared._canonical.decode("utf-8")
        except UnicodeError:
            raise _argument_error() from None
        values, _ = _prepare(self, raw)
        return (values,)


@dataclass(frozen=True, slots=True)
class PreparedTaskArgs:
    _spec: TaskSpec = field(repr=False)
    _canonical: bytes = field(repr=False)

    @property
    def digest(self):
        return hashlib.sha256(self._canonical).hexdigest()


def _prepare(spec, raw):
    if type(spec) is not TaskSpec:
        raise _schema_error()
    spec._check_schema()
    if type(raw) is str:
        try:
            if len(raw.encode("utf-8")) > 8192:
                raise _argument_error()
        except UnicodeError:
            raise _argument_error() from None
    if raw is None or (type(raw) is str and not raw.strip()):
        raw = {}
    try:
        _, encoded = closed_object(raw, max_bytes=8192)
        model = spec.args_model.model_validate_json(encoded, strict=True)
        return closed_object(model.model_dump(mode="json"), max_bytes=8192)
    except (BusinessException, ValidationError, ValueError, TypeError, RecursionError):
        raise _argument_error() from None


def prepare_task_args(spec, raw):
    """Validate supplied and default-expanded bytes without starting a task."""
    _, encoded = _prepare(spec, raw)
    return PreparedTaskArgs(spec, encoded)


@dataclass(frozen=True, slots=True)
class TaskContractRegistry:
    specs: tuple[TaskSpec, ...]
    _index: object = field(init=False, repr=False)

    def __post_init__(self):
        if (
            type(self.specs) is not tuple
            or len(self.specs) > 256
            or any(type(spec) is not TaskSpec for spec in self.specs)
            or len({spec.key for spec in self.specs}) != len(self.specs)
        ):
            raise _schema_error()
        object.__setattr__(
            self, "_index", MappingProxyType({spec.key: spec for spec in self.specs})
        )

    def resolve(self, key, *, scope):
        if type(key) is not str or type(scope) is not TaskScope:
            raise _schema_error()
        spec = self._index.get(key)
        if spec is None or spec.scope is not scope:
            raise _schema_error()
        spec._check_schema()
        return spec

    def tenant_descriptions(self):
        result = [
            {
                "key": spec.key,
                "labelKey": spec.label_key,
                "parameters": spec.public_schema(),
            }
            for spec in sorted(self.specs, key=lambda item: item.key)
            if spec.scope is TaskScope.TENANT
        ]
        closed_object(
            {"tasks": result}, max_bytes=64 * 1024, max_depth=16, max_nodes=16384
        )
        return result
