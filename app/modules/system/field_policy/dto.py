"""Explicit user output lineage. Inputs are normalized values, never ORM graphs."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from pydantic.alias_generators import to_camel

from app.core.exceptions import AuthorizationException, InvalidParameterException
from app.modules.system.field_policy.evaluator import (
    FieldDecisionSet,
    FieldGrant,
    project_user_values,
    resolve_record_fields,
)
from app.modules.system.field_policy.user_fields import USER_FIELDS


@dataclass(frozen=True, slots=True)
class OutputSource:
    field_id: str
    tables: tuple[str, ...]
    member: str | None = None


_SCALARS = (
    "user_id",
    "user_name",
    "nickname",
    "user_phone",
    "user_email",
    "user_gender",
    "status",
    "create_time",
)
_ROLE_MEMBERS = ("roles", "role_names", "role_name_keys")
_DEPT_MEMBERS = ("dept_ids", "dept_names", "primary_dept", "user_depts")


def _sources(fields):
    return {to_camel(name): OutputSource(name, ("sys_user",)) for name in fields}


USER_OUTPUT_SOURCES = MappingProxyType(
    {
        "list": MappingProxyType(
            {
                **_sources(_SCALARS),
                **{
                    to_camel(name): OutputSource(
                        "roles", ("sys_user_role", "sys_role"), name
                    )
                    for name in _ROLE_MEMBERS
                },
                **{
                    to_camel(name): OutputSource(
                        "departments", ("sys_user_dept", "sys_dept"), name
                    )
                    for name in _DEPT_MEMBERS
                },
            }
        ),
        "profile": MappingProxyType(
            {
                **_sources((*_SCALARS, "user_avatar")),
                "roles": OutputSource("roles", ("sys_user_role", "sys_role")),
            }
        ),
        "email": MappingProxyType(
            {
                "email": OutputSource("user_email", ("sys_user",)),
                "verified": OutputSource("email_verified", ("sys_user",)),
            }
        ),
        "avatar": MappingProxyType(_sources(("user_avatar",))),
        "identity": MappingProxyType(_sources(("user_id", "user_name", "nickname"))),
        "members": MappingProxyType(
            {
                **_sources(("user_id", "user_name", "nickname", "status")),
                **{
                    to_camel(name): OutputSource(
                        "departments", ("sys_user_dept",), name
                    )
                    for name in ("is_member", "is_primary")
                },
            }
        ),
    }
)

_ENTRYPOINT = {
    "list": "user-list",
    "identity": "user-list",
    "profile": "own-profile",
    "email": "own-profile",
    "avatar": "own-profile",
    "members": "user-list",
}


def _invalid():
    return InvalidParameterException(
        "用户字段输出来源无效", error_code="FIELD_POLICY_INVALID"
    )


def _wire(value):
    if isinstance(value, Mapping):
        return {to_camel(key): _wire(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_wire(item) for item in value]
    return value


def _validate_relations(values):
    for root, members in (("roles", _ROLE_MEMBERS), ("departments", _DEPT_MEMBERS)):
        if root not in values:
            continue
        value = values[root]
        if not isinstance(value, Mapping) or not value.keys() <= set(members):
            raise _invalid()
        for name, items in value.items():
            if name in {"dept_names", "primary_dept"}:
                if items is not None and not isinstance(items, str):
                    raise _invalid()
            elif not isinstance(items, (tuple, list)):
                raise _invalid()
            elif name == "user_depts":
                for item in items:
                    if (
                        not isinstance(item, Mapping)
                        or set(item) != {"dept_id", "is_primary"}
                        or not isinstance(item["dept_id"], str)
                        or not item["dept_id"].isascii()
                        or not item["dept_id"].isdigit()
                        or type(item["is_primary"]) is not bool
                    ):
                        raise _invalid()
            elif any(
                not isinstance(item, str)
                and not (name == "role_name_keys" and item is None)
                for item in items
            ):
                raise _invalid()


def project_user_dto(
    *,
    view,
    values,
    decisions,
    capability_decisions=(),
    function_permissions=frozenset(),
):
    sources = USER_OUTPUT_SOURCES.get(view)
    if sources is None or decisions.entrypoint != _ENTRYPOINT[view]:
        raise _invalid()
    fields = {source.field_id for source in sources.values()}
    if not isinstance(values, Mapping) or not values.keys() <= fields:
        raise _invalid()
    if view == "list":
        _validate_relations(values)
    if view == "profile" and "roles" in values:
        roles = values["roles"]
        if not isinstance(roles, (list, tuple)) or any(
            not isinstance(role, str) for role in roles
        ):
            raise _invalid()
    if view == "members" and "departments" in values:
        relation = values["departments"]
        if (
            not isinstance(relation, Mapping)
            or not relation.keys() <= {"is_member", "is_primary"}
            or any(type(value) is not bool for value in relation.values())
        ):
            raise _invalid()
    projection = project_user_values(
        raw_values=values,
        decisions=decisions,
        entrypoint_caps=dict.fromkeys(fields, "plain"),
    )
    result = {}
    for wire_name, source in sources.items():
        if source.field_id not in projection.values:
            continue
        value = projection.values[source.field_id]
        if source.member is not None:
            if source.member not in value:
                continue
            value = value[source.member]
        result[wire_name] = _wire(value)
    result["fieldAccess"] = {
        to_camel(name): {
            "read": access.read,
            "maskId": access.mask_id,
            **_capabilities(
                name, view, decisions, capability_decisions, function_permissions
            ),
        }
        for name, access in sorted(projection.field_access.items())
    }
    return result


_QUERY_CAPS = {
    "user_name": frozenset({"contains"}),
    "nickname": frozenset({"contains"}),
    "user_phone": frozenset({"contains"}),
    "user_email": frozenset({"contains"}),
    "user_gender": frozenset({"eq"}),
    "status": frozenset({"eq"}),
    "roles": frozenset({"eq"}),
}


def _capabilities(name, view, read_decisions, capability_decisions, permissions):
    result = {"create": False, "update": False, "replace": False, "queryOps": []}
    if type(capability_decisions) is not tuple or type(permissions) is not frozenset:
        raise _invalid()
    own = view in {"profile", "email", "avatar"}
    for decision in capability_decisions:
        if (
            type(decision) is not FieldDecisionSet
            or decision.entrypoint != read_decisions.entrypoint
        ):
            raise _invalid()
        operation = decision.operation
        if not decision.fields[name].allowed:
            continue
        if own:
            if operation == "update" and decision.purpose == "own-account":
                result["update"] = True
            continue
        if view == "members":
            if (
                name == "departments"
                and operation == "replace"
                and decision.purpose == "management.relations"
            ):
                required = {"system:dept:list", "system:dept:edit", "system:user:edit"}
                result["replace"] = "*" in permissions or required <= permissions
            if (
                name in {"user_name", "nickname"}
                and operation == "contains"
                and decision.purpose == "management.query"
            ):
                result["queryOps"].append(operation)
            continue
        if view != "list":
            continue
        required = {
            "create": {"system:user:add"},
            "update": {"system:user:edit"},
            "replace": {
                "system:user:edit",
                "system:user:role-auth" if name == "roles" else "system:dept:list",
            },
        }
        if operation in required and decision.purpose == (
            "management.relations"
            if operation == "replace"
            else f"management.{operation}"
        ):
            result[operation] = "*" in permissions or required[operation] <= permissions
        if (
            decision.purpose == "management.query"
            and operation in _QUERY_CAPS.get(name, ())
            and ("*" in permissions or "system:user:list" in permissions)
        ):
            result["queryOps"].append(operation)
    result["queryOps"] = sorted(set(result["queryOps"]))
    return result


_OWN_READ = (*_SCALARS, "user_avatar", "roles", "email_verified")
_OWN_UPDATE = ("nickname", "user_gender", "user_phone", "user_email", "user_avatar")
_OWN_SOURCE = "own-account/v1"


def resolve_own_account_fields(
    *, actor, target, grants, applicable_role_ids, operation
):
    """Code-registered self grant; never a role or purpose supplied by a client."""
    if (
        type(actor) is not tuple
        or len(actor) != 2
        or any(type(part) is not int for part in actor)
        or actor[0] < 0
        or actor[1] <= 0
        or target != actor
        or type(target) is not tuple
        or len(target) != 2
        or any(type(part) is not int for part in target)
        or operation not in {"read", "update"}
        or _OWN_SOURCE in applicable_role_ids
        or any(rule.role_id == _OWN_SOURCE for rule in grants)
    ):
        raise AuthorizationException(
            "只能访问本人字段",
            error_code="FIELD_WRITE_DENIED"
            if operation == "update"
            else "FIELD_READ_DENIED",
        )
    fields = _OWN_READ if operation == "read" else _OWN_UPDATE
    own = tuple(
        FieldGrant(_OWN_SOURCE, name, operation, "own-account") for name in fields
    )
    return resolve_record_fields(
        catalog=USER_FIELDS,
        trusted_grants=(*grants, *own),
        applicable_role_ids=applicable_role_ids | frozenset({_OWN_SOURCE}),
        operation=operation,
        purpose="own-account",
        entrypoint="own-profile",
    )
