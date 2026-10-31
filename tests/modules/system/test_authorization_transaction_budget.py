"""Real pool, barrier, and database-error boundaries for planned transactions."""

import asyncio
from contextlib import suppress

import pytest
from sqlalchemy import event, literal, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    ResourceKey,
    ResourceKind,
    TenantLock,
    TransactionBudget,
)
from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard as Guard,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyState as State,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def _cancel(task):
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_total_deadline_includes_waiting_for_connection_pool(plan_sessions):
    _, plain = plan_sessions
    async with plain() as db:
        schema = await db.scalar(text("SELECT current_schema()"))
    pooled = create_async_engine(
        plain.kw["bind"].url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=3,
        connect_args={"server_settings": {"search_path": schema}},
    )
    factory = async_sessionmaker(
        pooled,
        class_=AuthorizationPlanSession,
        expire_on_commit=False,
        autoflush=False,
    )
    try:
        async with pooled.connect(), factory() as db:
            started = asyncio.get_running_loop().time()
            with pytest.raises(BusinessException) as error:
                async with transactions.scope(
                    db,
                    tenants=(TenantLock(0),),
                    budget=TransactionBudget(timeout_ms=180),
                ):
                    pytest.fail("the only pooled connection is still checked out")
            elapsed = asyncio.get_running_loop().time() - started
            assert error.value.error_code == "AUTHORIZATION_TRANSACTION_TIMEOUT"
            # The pool itself would wait three seconds. The outer budget owns it.
            assert elapsed < 1.5
            assert pooled.pool.checkedout() == 1
            with pytest.raises(BusinessException):
                await db.commit()
            await db.rollback()
    finally:
        await pooled.dispose()


async def test_global_and_tenant_waits_share_one_barrier_budget(plan_sessions):
    factory, plain = plan_sessions
    g_waiting = asyncio.Event()
    b_waiting = asyncio.Event()
    entered = asyncio.Event()
    contender = None

    def observe(_conn, _cursor, sql, _params, _context, _many):
        if "FOR SHARE" in sql:
            if "sys_user_field_policy_global_guard" in sql:
                g_waiting.set()
            elif "sys_user_field_policy_state" in sql:
                b_waiting.set()

    async def attempt():
        async with factory() as db:
            started = asyncio.get_running_loop().time()
            try:
                async with transactions.scope(
                    db,
                    tenants=(TenantLock(0),),
                    budget=TransactionBudget(timeout_ms=3000, lock_timeout_ms=600),
                ) as tx:
                    entered.set()
                    await tx.lock(LockPlan())
            except BusinessException as exc:
                elapsed = asyncio.get_running_loop().time() - started
                with pytest.raises(BusinessException):
                    await db.commit()
                await db.rollback()
                return exc.code, exc.error_code, elapsed
            await db.commit()
            return 200, "committed", asyncio.get_running_loop().time() - started

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", observe)
    try:
        async with plain() as g_holder, plain() as b_holder:
            await g_holder.execute(
                select(Guard.guard_id).where(Guard.guard_id == 1).with_for_update()
            )
            await b_holder.execute(
                select(State.tenant_id).where(State.tenant_id == 0).with_for_update()
            )
            contender = asyncio.create_task(attempt())
            await asyncio.wait_for(g_waiting.wait(), 2)
            await asyncio.sleep(0.35)
            await g_holder.rollback()
            await asyncio.wait_for(b_waiting.wait(), 2)
            await asyncio.sleep(0.35)
            await b_holder.rollback()
            status, outcome, elapsed = await asyncio.wait_for(contender, 2)
            # Resetting 600 ms at B would incorrectly admit both 350 ms waits.
            # PostgreSQL lock_timeout and the outer barrier timeout race at the
            # same boundary; both must fail closed, forbid commit, and stay bounded.
            assert status == 503
            assert outcome in {
                "FIELD_POLICY_UNAVAILABLE",
                "AUTHORIZATION_TRANSACTION_TIMEOUT",
            }
            assert elapsed < 1.5
            assert not entered.is_set()
    finally:
        await _cancel(contender)
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", observe)


@pytest.mark.parametrize("method", ["scalar", "execute", "connection_scalar"])
@pytest.mark.parametrize("inside_savepoint", [False, True])
async def test_caught_typed_database_error_cannot_restore_transaction(
    plan_sessions, method, inside_savepoint
):
    factory, _ = plan_sessions
    async with factory() as db:

        async def caught_database_error():
            statement = select(literal(1) / literal(0))
            with pytest.raises(DBAPIError) as failure:
                if method == "connection_scalar":
                    connection = await db.connection()
                    await connection.scalar(statement)
                else:
                    await getattr(db, method)(statement)
            assert failure.value.orig.sqlstate == "22012"

        with pytest.raises(BusinessException) as failure:
            async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
                await tx.lock(LockPlan())
                if inside_savepoint:
                    with pytest.raises(BusinessException) as nested_failure:
                        async with tx.savepoint():
                            await caught_database_error()
                    assert nested_failure.value.error_code == "FIELD_POLICY_LOCK_ORDER"
                else:
                    await caught_database_error()
                with pytest.raises(BusinessException):
                    tx.assert_held()
        assert failure.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()
        async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            await tx.lock(LockPlan())
            assert await db.scalar(select(literal(1))) == 1
        await db.commit()


async def test_global_scope_refuses_missing_barrier_without_repair(plan_sessions):
    factory, plain = plan_sessions
    async with plain() as db:
        # This disposable schema deliberately simulates damaged storage. DELETE
        # is protected by the product trigger and is not a repair mechanism.
        await db.execute(text("TRUNCATE TABLE sys_user_field_policy_state"))
        await db.commit()
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            async with transactions.scope_global(db):
                pytest.fail("global enumeration accepted missing tenant barriers")
        assert error.value.error_code == "FIELD_POLICY_UNAVAILABLE"
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()
    async with plain() as db:
        assert (await db.scalars(select(State.tenant_id))).all() == []


async def test_global_exclusive_guard_blocks_another_tenant(plan_sessions):
    factory, plain = plan_sessions
    g_waiting = asyncio.Event()
    entered = asyncio.Event()
    contender = None

    def observe(_conn, _cursor, sql, _params, _context, _many):
        if "sys_user_field_policy_global_guard" in sql and "FOR SHARE" in sql:
            g_waiting.set()

    async def attempt():
        async with factory() as db:
            async with transactions.scope(db, tenants=(TenantLock(1),)) as tx:
                entered.set()
                await tx.lock(
                    LockPlan(existing=(ResourceKey(ResourceKind.USER, 1, 102),))
                )
            await db.commit()

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", observe)
    try:
        async with factory() as holder:
            async with transactions.scope(
                holder,
                tenants=(TenantLock(0),),
                global_mode=LockMode.EXCLUSIVE,
            ) as tx:
                await tx.lock(LockPlan())
                contender = asyncio.create_task(attempt())
                await asyncio.wait_for(g_waiting.wait(), 2)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(contender), 0.12)
                assert not entered.is_set()
            # Scope exit alone must retain the physical guard until its owner ends.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(contender), 0.12)
            await holder.rollback()
            await asyncio.wait_for(contender, 2)
            assert entered.is_set()
    finally:
        await _cancel(contender)
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", observe)


async def test_exclusive_tenant_barriers_allow_other_tenants_in_parallel(plan_sessions):
    factory, _ = plan_sessions
    entered = asyncio.Event()

    async def other_tenant():
        async with factory() as db:
            async with transactions.scope(
                db, tenants=(TenantLock(1, LockMode.EXCLUSIVE),)
            ) as tx:
                await tx.lock(
                    LockPlan(existing=(ResourceKey(ResourceKind.USER, 1, 102),))
                )
                entered.set()
            await db.commit()

    async with factory() as holder:
        async with transactions.scope(
            holder, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            user = ResourceKey(ResourceKind.USER, 0, 101)
            await tx.lock(LockPlan(existing=(user,)))
            # The second transaction finishes while the first still holds B-X.
            await asyncio.wait_for(other_tenant(), 1.5)
            assert entered.is_set()
            tx.assert_held(user)
        await holder.commit()
