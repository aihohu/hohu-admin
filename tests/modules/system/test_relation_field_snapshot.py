"""Real protected transactions share immutable facts, never mutable admission."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select

from app.core.authorization_plan import LockMode, TenantLock
from app.core.exceptions import BusinessException
from app.core.tenant_scope import TenantContext
from app.modules.system.models.user import User
from app.modules.system.service import generic_field_policy_service as module
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from tests.modules.system.test_generic_policy_transitions import (  # noqa: F401
    command,
    generic_sessions,
    ready,
    run,
    transactions,
)
from tests.modules.system.test_generic_policy_transitions import (
    plan_sessions as _plan_sessions,
)
from tests.modules.system.test_generic_policy_transitions import (
    policies as _policies,
)

plan_sessions = _plan_sessions
policies = _policies


async def test_same_transaction_verifies_manifest_only_once(policies, monkeypatch):
    proof, marked = await ready(policies, monkeypatch)
    await run(
        policies,
        command("activate", 4, expected_epoch=marked["epoch"]),
        qualification=proof,
    )
    original = module.read_manifest
    spy = AsyncMock(wraps=original)
    monkeypatch.setattr(module, "read_manifest", spy)
    factory, _, catalog = policies
    for _ in range(2):
        async with factory() as db:
            async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
                for roles in (("201",), (), ("999",)):
                    assert (
                        await module.generic_field_policy_service.load_active(
                            db,
                            tx,
                            tenant_id=0,
                            catalog=catalog,
                            object_id="sample.item",
                            role_ids=roles,
                        )
                        == ()
                    )
            await db.rollback()
    assert spy.await_count == 2


async def test_select_budget_rejects_before_extra_query(plan_sessions):
    factory, _ = plan_sessions
    budget = AuthorizationReadBudget()
    budget.max_statements = 1
    async with factory() as db:
        engine = factory.kw["bind"].sync_engine
        physical = []

        def executed(_conn, _cursor, statement, _parameters, _context, _many):
            physical.append(statement)

        event.listen(engine, "before_cursor_execute", executed)
        with bounded_authorization_reads(db, budget):
            await db.execute(select(User.user_id).limit(1))
            with pytest.raises(BusinessException) as exc:
                await db.execute(select(User.user_id).limit(1))
            assert exc.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
            assert budget.statements == 1
            assert len(physical) == 1
        event.remove(engine, "before_cursor_execute", executed)
        await db.rollback()


async def test_live_freeze_invalidates_shared_snapshot(policies, monkeypatch):
    proof, marked = await ready(policies, monkeypatch)
    active = await run(
        policies,
        command("activate", 4, expected_epoch=marked["epoch"]),
        qualification=proof,
    )
    factory, _, catalog = policies
    service = module.generic_field_policy_service
    tenant = TenantContext(0, "plan-0", 101, 1, "access_token")
    async with factory() as db:
        async with transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            await service.load_active(
                db,
                tx,
                tenant_id=0,
                catalog=catalog,
                object_id="sample.item",
                role_ids=("201",),
            )
            plan = await service.plan(
                db,
                tx,
                tenant=tenant,
                catalog=catalog,
                command=command(
                    "freeze", 5, expected_epoch=active["epoch"], object_id="sample.item"
                ),
            )
            await tx.lock(plan.lock_plan)
            await service.apply(db, tx, plan, tenant=tenant, catalog=catalog)
            with pytest.raises(BusinessException) as exc:
                await service.load_active(
                    db,
                    tx,
                    tenant_id=0,
                    catalog=catalog,
                    object_id="sample.item",
                    role_ids=("201",),
                )
            assert exc.value.error_code == "FIELD_POLICY_UNAVAILABLE"
        await db.rollback()


async def test_cached_read_is_two_selects_and_cannot_cross_barrier(
    policies, monkeypatch
):
    proof, marked = await ready(policies, monkeypatch)
    await run(
        policies,
        command("activate", 4, expected_epoch=marked["epoch"]),
        qualification=proof,
    )
    factory, _, catalog = policies
    service = module.generic_field_policy_service
    async with factory() as db:
        async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            budget = AuthorizationReadBudget(max_statements=9)
            with bounded_authorization_reads(db, budget):
                await service.load_active(
                    db,
                    tx,
                    tenant_id=0,
                    catalog=catalog,
                    object_id="sample.item",
                    role_ids=("201",),
                )
                assert budget.statements == 7
                await service.load_active(
                    db,
                    tx,
                    tenant_id=0,
                    catalog=catalog,
                    object_id="sample.item",
                    role_ids=(),
                )
                assert budget.statements == 9
            with pytest.raises(BusinessException):
                await service.load_active(
                    db,
                    tx,
                    tenant_id=1,
                    catalog=catalog,
                    object_id="sample.item",
                    role_ids=("201",),
                )
        await db.rollback()
        with pytest.raises(BusinessException):
            await service.load_active(
                db,
                tx,
                tenant_id=0,
                catalog=catalog,
                object_id="sample.item",
                role_ids=("201",),
            )


async def test_nested_select_budget_cannot_reset_outer(plan_sessions):
    factory, _ = plan_sessions
    async with factory() as db:
        outer = AuthorizationReadBudget(max_statements=1)
        with bounded_authorization_reads(db, outer):
            await db.execute(select(User.user_id).limit(1))
            with bounded_authorization_reads(
                db, AuthorizationReadBudget(max_statements=100)
            ):
                with pytest.raises(BusinessException):
                    await db.execute(select(User.user_id).limit(1))
        assert outer.statements == 1
        await db.rollback()
