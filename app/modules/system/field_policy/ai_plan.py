"""Field dependencies of the existing AI tool contracts, never arbitrary SQL."""

from app.core.exceptions import AuthorizationException
from app.modules.system.field_policy.query_plan import (
    QueryDependency,
    UserQueryPlan,
    query_denied,
)

UPDATE_FIELDS = frozenset(
    {"nickname", "user_email", "user_phone", "user_gender", "status"}
)


def ai_query_plan(tool, args):
    if type(args) is not dict:
        raise query_denied()
    dependencies = []
    filters = args.get("filters") or {}
    if type(filters) is not dict or not set(filters) <= {"status", "user_gender"}:
        raise query_denied()
    dependencies.extend(
        QueryDependency(name, "eq", "filter") for name in sorted(filters)
    )
    if tool == "user.lookup":
        for key, field in (
            ("user_name", "user_name"),
            ("nickname", "nickname"),
            ("phone", "user_phone"),
            ("email", "user_email"),
        ):
            if args.get(key) is not None:
                dependencies.append(QueryDependency(field, "exact", "filter"))
    elif tool == "user.batch_delete":
        for key, field in (("user_names", "user_name"), ("phones", "user_phone")):
            if args.get(key):
                dependencies.append(QueryDependency(field, "exact", "filter"))
    elif tool in {"user.stats", "user.distinct"}:
        field = args.get("group_by" if tool == "user.stats" else "field")
        if field not in {"status", "user_gender"}:
            raise query_denied()
        operation = "group" if tool == "user.stats" else "distinct"
        dependencies.append(QueryDependency(field, operation, operation))
    return UserQueryPlan(
        tuple(dependencies), fixed_order=("user_id:asc",) if tool == "user.list" else ()
    )


def ai_write_fields(tool, args):
    def refuse():
        raise AuthorizationException(
            "写入字段未获授权", error_code="FIELD_WRITE_DENIED"
        )

    if type(args) is not dict:
        refuse()
    if tool == "user.update":
        if set(args) - UPDATE_FIELDS - {"user_id"}:
            refuse()
        return {
            "update": frozenset(
                name for name in UPDATE_FIELDS if args.get(name) is not None
            )
        }
    if tool == "user.create":
        if (
            set(args)
            - UPDATE_FIELDS
            - {"user_name", "primary_dept_id", "role_assignment"}
        ):
            refuse()
        # Password/status/gender are server defaults when omitted, still writes.
        fields = {"user_name", "password", "status", "user_gender"} | {
            name for name in UPDATE_FIELDS if args.get(name) is not None
        }
        result = {"create": frozenset(fields)}
        relations = {"roles"}
        if args.get("primary_dept_id") is not None:
            relations.add("departments")
        result["replace"] = frozenset(relations)
        return result
    if tool == "user.reset_password":
        return {"update": frozenset({"password"})}
    if tool in {"user.update_roles", "user.update_dept"}:
        return {
            "replace": frozenset(
                {"roles" if tool == "user.update_roles" else "departments"}
            )
        }
    return {}
