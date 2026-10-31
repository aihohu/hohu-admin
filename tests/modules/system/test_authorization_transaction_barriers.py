"""Complete barrier sets and real transaction budgets in disposable schemas."""

import asyncio

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

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
from app.db.session import engine
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from tests.modules.system.field_policy_test_support import isolated_field_policy_schema


@pytest.fixture
async def plan_sessions():
    base = create_async_engine(engine.url, poolclass=NullPool)
    try:
        async with isolated_field_policy_schema(base) as scoped:
            plain = async_sessionmaker(scoped, expire_on_commit=False, autoflush=False)
            async with plain() as db:
                for tenant_id in (0, 1):
                    await db.execute(
                        insert(Tenant).values(
                            tenant_id=tenant_id,
                            tenant_code=f"plan-{tenant_id}",
                            tenant_name="plan",
                            status="1",
                            lifecycle_state="active",
                            bootstrap_version=1,
                            row_version=1,
                            bootstrap_key_hash="a" * 64 if tenant_id else None,
                            bootstrap_fingerprint="b" * 64 if tenant_id else None,
                        )
                    )
                    await db.execute(
                        insert(User).values(
                            tenant_id=tenant_id,
                            user_id=101 + tenant_id,
                            user_name="planner",
                            hashed_password="not-a-credential",
                            status="1",
                        )
                    )
                    await db.execute(
                        insert(Role).values(
                            tenant_id=tenant_id,
                            role_id=201 + tenant_id,
                            role_name="planner",
                            role_code="PLAN",
                            status="1",
                            data_scope="1",
                        )
                    )
                await db.commit()
            yield (
                async_sessionmaker(
                    scoped,
                    class_=AuthorizationPlanSession,
                    expire_on_commit=False,
                    autoflush=False,
                ),
                plain,
            )
    finally:
        await base.dispose()


async def test_plan_holds_actual_domain_rows_until_owner_finishes(plan_sessions):
    factory, plain = plan_sessions
    user = ResourceKey(ResourceKind.USER, 0, 101)
    async with factory() as db, plain() as observer:
        async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            await tx.lock(LockPlan(existing=(user,)))
            tx.assert_held(user)
            contender = asyncio.create_task(
                observer.execute(
                    select(User.user_id).where(User.user_id == 101).with_for_update()
                )
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(contender), 0.1)
        await db.rollback()
        assert (await asyncio.wait_for(contender, 2)).scalar_one() == 101


async def test_mixed_tenants_follow_id_order_and_complete_plan_order(plan_sessions):
    factory, plain = plan_sessions
    seen = []
    async with factory() as db:

        def record(_conn, _cursor, sql, _params, _context, _many):
            if "FOR " in sql and "SELECT" in sql:
                seen.append(sql)

        event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
        async with transactions.scope(
            db,
            tenants=(
                TenantLock(1, LockMode.EXCLUSIVE),
                TenantLock(0, LockMode.SHARED),
            ),
        ) as tx:
            await tx.lock(
                LockPlan(
                    existing=(
                        ResourceKey(ResourceKind.USER, 0, 101),
                        ResourceKey(ResourceKind.ROLE, 1, 202),
                        ResourceKey(ResourceKind.ROLE, 0, 201),
                    )
                )
            )
        assert "FOR SHARE" in seen[1] and "FOR UPDATE" in seen[2]
        assert "sys_role" in seen[3] and "sys_role" in seen[4]
        assert "sys_user." in seen[5]
        await db.commit()


async def test_missing_domain_row_never_returns_partial_proof(plan_sessions):
    factory, _ = plan_sessions
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
                await tx.lock(
                    LockPlan(
                        existing=(
                            ResourceKey(ResourceKind.ROLE, 0, 201),
                            ResourceKey(ResourceKind.USER, 0, 999),
                        )
                    )
                )
        assert error.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
        with pytest.raises(BusinessException):
            tx.assert_held(ResourceKey(ResourceKind.ROLE, 0, 201))
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()


async def test_outer_deadline_includes_work_between_statements(plan_sessions):
    factory, _ = plan_sessions
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            async with transactions.scope(
                db, tenants=(TenantLock(0),), budget=TransactionBudget(timeout_ms=180)
            ):
                await db.scalar(select(User.user_id).where(User.user_id == 101))
                await asyncio.sleep(0.3)
        assert error.value.error_code == "AUTHORIZATION_TRANSACTION_TIMEOUT"
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()
        async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            await tx.lock(LockPlan())


@pytest.mark.parametrize("count", [33, 1024, 1025])
async def test_global_scope_enumerates_all_states_and_enforces_total_limit(
    plan_sessions,
    count,
):
    factory, plain = plan_sessions
    async with plain() as db:
        await db.execute(
            insert(Tenant),
            [
                {
                    "tenant_id": i,
                    "tenant_code": f"global-{i}",
                    "tenant_name": "global",
                    "status": "2",
                    "lifecycle_state": "prepared" if i % 2 else "disabled",
                    "bootstrap_version": 0,
                    "row_version": 1,
                }
                for i in range(2, count)
            ],
        )
        await db.commit()
    async with factory() as db:
        if count > 1024:
            with pytest.raises(BusinessException) as error:
                async with transactions.scope_global(db):
                    pytest.fail("oversize set returned a transaction")
            assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
        else:
            async with transactions.scope_global(db) as tx:
                await tx.lock(
                    LockPlan(existing=(ResourceKey(ResourceKind.USER, 1, 102),))
                )
            await db.commit()
