"""Closed user query dependencies and per-role coverage predicates.

Only trusted adapters supply role scopes and grants. A returned predicate is
not a data filter: it is used to find a counterexample in the entire authorized
base range, before any client predicate, pagination or aggregate executes.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import and_, false, not_, or_, true
from sqlalchemy.sql.elements import ColumnElement

from app.core.exceptions import AuthorizationException
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.user_fields import USER_FIELDS


def query_denied():
    return AuthorizationException(
        "查询依赖字段未获完整范围授权", error_code="FIELD_QUERY_DENIED"
    )


@dataclass(frozen=True, slots=True)
class QueryDependency:
    field_id: str
    operation: str
    use: str
    purpose: str = "management.query"

    def __post_init__(self):
        field = USER_FIELDS.get(self.field_id)
        if (
            field is None
            or self.purpose not in {"management.query", "import"}
            or (self.purpose == "import" and self.operation != "exact")
            or (
                field.query_purposes is not None
                and self.purpose not in field.query_purposes
            )
            or self.use not in {"filter", "join", "group", "distinct", "fixed_order"}
            or (
                self.operation not in field.query_ops
                and not (
                    self.use == "fixed_order"
                    and self.field_id in {"create_time", "user_id"}
                    and self.operation == "read"
                )
            )
        ):
            raise query_denied()


_FILTERS = {
    "user_name": ("user_name", "contains", "filter"),
    "nickname": ("nickname", "contains", "filter"),
    "user_phone": ("user_phone", "contains", "filter"),
    "user_email": ("user_email", "contains", "filter"),
    "user_gender": ("user_gender", "eq", "filter"),
    "status": ("status", "eq", "filter"),
    "role_code": ("roles", "eq", "join"),
}


@dataclass(frozen=True, slots=True)
class UserQueryPlan:
    dependencies: tuple[QueryDependency, ...]
    fixed_order: tuple[str, ...] = ("create_time:desc", "user_id:desc")
    counts_base_range: bool = True

    @classmethod
    def for_list(cls, supplied_filters: frozenset[str]):
        if (
            type(supplied_filters) is not frozenset
            or not supplied_filters <= _FILTERS.keys()
        ):
            raise query_denied()
        return cls(
            tuple(
                QueryDependency(*definition)
                for name, definition in _FILTERS.items()
                if name in supplied_filters
            )
        )

    @property
    def all_dependencies(self):
        # The fixed metadata ordering is declared, not a client sort capability.
        # create_time still needs read coverage; structural user_id is always safe.
        return self.dependencies + tuple(
            QueryDependency(order.split(":")[0], "read", "fixed_order")
            for order in self.fixed_order
        )

    @classmethod
    def for_members(cls, *, search: bool):
        if type(search) is not bool:
            raise query_denied()
        return cls(
            (
                (
                    QueryDependency("user_name", "contains", "filter"),
                    QueryDependency("nickname", "contains", "filter"),
                )
                if search
                else ()
            )
            + (
                QueryDependency("status", "eq", "filter"),
                QueryDependency("departments", "eq", "join"),
            ),
            fixed_order=("user_id:asc",),
        )


def query_permission_predicate(
    dependency: QueryDependency,
    *,
    grants: tuple[FieldGrant, ...],
    role_scopes: Mapping[str, ColumnElement],
):
    """Union individual allows, then subtract every applicable explicit deny."""
    if (
        type(dependency) is not QueryDependency
        or type(grants) is not tuple
        or any(type(rule) is not FieldGrant for rule in grants)
        or any(not isinstance(scope, ColumnElement) for scope in role_scopes.values())
    ):
        raise query_denied()
    if dependency.field_id == "user_id" and dependency.operation == "read":
        return true()
    purpose = (
        "management.read" if dependency.operation == "read" else dependency.purpose
    )
    allows, denies = [], []
    for rule in grants:
        if (
            rule.field_id != dependency.field_id
            or rule.operation != dependency.operation
            or rule.purpose != purpose
            or rule.role_id not in role_scopes
        ):
            continue
        (denies if rule.effect == "deny" else allows).append(role_scopes[rule.role_id])
    return and_(or_(false(), *allows), not_(or_(false(), *denies)))
