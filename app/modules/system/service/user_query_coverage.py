"""Authorize all query dependencies before client predicates or aggregates."""

from sqlalchemy import select, true

from app.modules.system.field_policy.query_plan import (
    UserQueryPlan,
    query_denied,
    query_permission_predicate,
)
from app.modules.system.models.user import User


async def assert_user_query_coverage(
    db, tx, *, tenant_id, base_predicates, plan, grants, role_scopes
):
    """Find one unauthorized base record per dependency; never return its ID.

    base_predicates is server-owned record scope, not a client search/filter.
    This service cannot enable policy enforcement or create an authorization
    transaction. Its caller owns G/B and the cumulative read/time budgets.
    """
    tx.assert_session(db)
    tx.assert_planning(tenant_id)
    if type(plan) is not UserQueryPlan or type(base_predicates) is not tuple:
        raise query_denied()
    for dependency in plan.all_dependencies:
        permitted = query_permission_predicate(
            dependency, grants=grants, role_scopes=role_scopes
        )
        counterexample = await db.scalar(
            select(User.user_id)
            .where(
                User.tenant_id == tenant_id,
                *base_predicates,
                # SQL NULL is not proof of permission either.
                permitted.is_not(true()),
            )
            .limit(1)
        )
        if counterexample is not None:
            raise query_denied()
