"""Real PostgreSQL coverage checks include rows outside the eventual result."""

import pytest
from sqlalchemy import false, insert, literal, true

from app.core.authorization_plan import TenantLock
from app.core.exceptions import BusinessException
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.query_plan import UserQueryPlan
from app.modules.system.models.user import User
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.user_query_coverage import assert_user_query_coverage
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


def rules(role="1", **kwargs):
    return (
        FieldGrant(role, "create_time", "read", "management.read"),
        FieldGrant(role, "user_phone", "contains", "management.query", **kwargs),
    )


async def check(db, *, grants, scopes, tenant_id=0):
    async with authorization_transactions.scope(
        db, tenants=(TenantLock(tenant_id),)
    ) as tx:
        await assert_user_query_coverage(
            db,
            tx,
            tenant_id=tenant_id,
            base_predicates=(),
            plan=UserQueryPlan.for_list(frozenset({"user_phone"})),
            grants=grants,
            role_scopes=scopes,
        )


async def test_scope_union_must_not_expand_query_grant(plan_sessions):
    factory, plain = plan_sessions
    async with plain() as setup:
        await setup.execute(
            insert(User).values(
                tenant_id=0,
                user_id=103,
                user_name="other",
                hashed_password="unused",
                status="1",
            )
        )
        await setup.commit()
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await check(
                db, grants=rules(), scopes={"1": User.user_id == 101, "2": true()}
            )
        assert error.value.error_code == "FIELD_QUERY_DENIED"


async def test_tenant_boundary_and_total_coverage(plan_sessions):
    factory, _ = plan_sessions
    async with factory() as db:
        await check(db, grants=rules(), scopes={"1": User.user_id == 101})
    async with factory() as db:
        with pytest.raises(BusinessException):
            await check(
                db, grants=rules(), scopes={"1": User.user_id == 101}, tenant_id=1
            )


@pytest.mark.parametrize("scope", [false(), literal(None)])
async def test_false_or_unknown_scope_never_counts_as_covered(plan_sessions, scope):
    factory, _ = plan_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            await check(db, grants=rules(), scopes={"1": scope})


async def test_matching_deny_blocks_even_with_all_allow(plan_sessions):
    factory, _ = plan_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            await check(
                db,
                grants=rules() + rules("2", effect="deny"),
                scopes={"1": true(), "2": User.user_id == 101},
            )
