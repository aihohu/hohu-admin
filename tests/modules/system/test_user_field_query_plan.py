"""A query must authorize its whole base range before evaluating its predicate."""

import pytest
from sqlalchemy import column, false, select, true
from sqlalchemy.dialects import postgresql

from app.core.exceptions import BusinessException
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.query_plan import (
    QueryDependency,
    UserQueryPlan,
    query_permission_predicate,
)


def grant(role, field="user_phone", effect="allow"):
    return FieldGrant(role, field, "contains", "management.query", effect=effect)


def test_list_plan_declares_filter_join_fixed_order_and_total():
    plan = UserQueryPlan.for_list(frozenset({"user_phone", "role_code"}))
    assert plan.dependencies == (
        QueryDependency("user_phone", "contains", "filter"),
        QueryDependency("roles", "eq", "join"),
    )
    assert plan.fixed_order == ("create_time:desc", "user_id:desc")
    assert plan.counts_base_range is True


@pytest.mark.parametrize(
    "field", ["hashed_password", "tenant_id", "user_id", "order_by"]
)
def test_list_plan_rejects_unregistered_inputs(field):
    with pytest.raises(BusinessException) as error:
        UserQueryPlan.for_list(frozenset({field}))
    assert error.value.error_code == "FIELD_QUERY_DENIED"


def test_permission_uses_each_roles_scope_and_deny_dominates():
    dependency = QueryDependency("user_phone", "contains", "filter")
    predicate = query_permission_predicate(
        dependency,
        grants=(grant("allow"), grant("deny", effect="deny")),
        role_scopes={
            "allow": column("department") == 1,
            "deny": column("user_id") == 9,
        },
    )
    sql = str(select(1).where(predicate).compile(dialect=postgresql.dialect()))
    assert "department =" in sql and "user_id !=" in sql


def test_other_roles_scope_does_not_extend_a_field_grant():
    predicate = query_permission_predicate(
        QueryDependency("user_phone", "contains", "filter"),
        grants=(grant("self"),),
        role_scopes={"self": false(), "all": true()},
    )
    assert str(select(1).where(predicate)) == str(select(1).where(false()))


def test_unregistered_operation_and_missing_role_are_closed():
    with pytest.raises(BusinessException):
        QueryDependency("user_email", "raw_sql", "filter")
    predicate = query_permission_predicate(
        QueryDependency("user_phone", "contains", "filter"),
        grants=(grant("deleted"),),
        role_scopes={},
    )
    assert str(select(1).where(predicate)) == str(select(1).where(false()))


def test_members_plan_declares_search_candidate_status_and_membership():
    plan = UserQueryPlan.for_members(search=True)
    assert {item.field_id for item in plan.all_dependencies} == {
        "user_name",
        "nickname",
        "status",
        "departments",
        "user_id",
    }
    assert plan.fixed_order == ("user_id:asc",)


def test_import_only_query_field_cannot_authorize_management_query():
    with pytest.raises(BusinessException):
        QueryDependency("employee_no", "exact", "filter")
