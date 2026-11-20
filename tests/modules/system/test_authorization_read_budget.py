"""Read limits include ORM relationship materialization, without SQL escapes."""

import pytest
from sqlalchemy import func, select

from app.core.authorization_plan import LockPlan, TenantLock
from app.core.exceptions import BusinessException
from app.modules.system.models.user import User
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_exact_builtin_functions_are_safe_reads(plan_sessions):
    factory, _ = plan_sessions
    async with factory() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            await tx.lock(LockPlan())
            assert await db.scalar(select(func.pg_catalog.lower("HELLO"))) == "hello"
            assert await db.scalar(select(func.pg_catalog.concat("a", "b"))) == "ab"
            assert await db.scalar(select(func.pg_catalog.count(User.user_id))) == 2


@pytest.mark.parametrize(
    "statement",
    [
        select(func.lower("HELLO")),
        select(func.pg_catalog.pg_advisory_xact_lock(1)),
        select(func.public.lower("HELLO")),
        select(func.count(User.user_id)),
        select(func.public.count(User.user_id)),
        select(func.pg_catalog.count(func.pg_catalog.pg_advisory_xact_lock(1))),
        select(func.pg_catalog.count()),
    ],
)
async def test_unknown_or_search_path_functions_still_rejected(
    plan_sessions, statement
):
    factory, _ = plan_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                await tx.lock(LockPlan())
                await db.execute(statement)
        await db.rollback()


async def test_row_and_byte_budget_fail_closed(plan_sessions):
    factory, _ = plan_sessions
    async with factory() as db:
        with bounded_authorization_reads(db, AuthorizationReadBudget(max_rows=1)):
            with pytest.raises(BusinessException) as error:
                await db.execute(select(User.user_id))
            assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
        await db.rollback()


async def test_decoded_byte_budget_fail_closed(plan_sessions):
    factory, _ = plan_sessions
    async with factory() as db:
        with bounded_authorization_reads(db, AuthorizationReadBudget(max_bytes=1)):
            with pytest.raises(BusinessException):
                await db.execute(select(User.user_name).where(User.user_id == 101))
        await db.rollback()


async def test_caught_read_limit_invalidates_live_proof(plan_sessions):
    factory, _ = plan_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                await tx.lock(LockPlan())
                with bounded_authorization_reads(
                    db, AuthorizationReadBudget(max_rows=0)
                ):
                    with pytest.raises(BusinessException):
                        await db.execute(
                            select(User.user_id).where(User.user_id == 101)
                        )
                tx.assert_held()
        await db.rollback()
