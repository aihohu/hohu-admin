"""Source locks, cumulative budgets and independent role scopes are enforceable."""

from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    ResourceKey,
    TenantLock,
)
from app.core.authorization_plan import (
    ResourceKind as K,
)
from app.core.exceptions import BusinessException
from app.core.field_policy import FieldRef, Grant, compile_rules
from app.modules.system.field_policy.ordinary_catalog import (
    production_catalog as relation_catalog,
)
from app.modules.system.field_policy.relation_catalog import UPDATE
from app.modules.system.models.dept import Dept
from app.modules.system.schemas.dept import DeptCreate
from app.modules.system.schemas.user import UserPatch
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate as service,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from app.modules.system.service.relation_field_write_guard import RelationWriteContext
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_relation_field_writes import (
    plan_sessions as _plan_sessions,
)
from tests.modules.system.test_relation_field_writes import (
    system_sessions as _system_sessions,
)

plan_sessions = _plan_sessions
system_sessions = _system_sessions


def test_wide_record_scope_cannot_lend_another_roles_field_grant():
    catalog = relation_catalog()
    context = RelationWriteContext(
        TENANT,
        SimpleNamespace(user_id=101),
        compile_rules(
            catalog, (Grant(0, "202", FieldRef("system.user", "nickname"), UPDATE),)
        ),
        {
            "201": SimpleNamespace(unbounded=True),
            "202": SimpleNamespace(
                unbounded=False, include_self=True, accessible_dept_ids=()
            ),
        },
        frozenset({"system:user:edit"}),
        {K.USER_DEPTS: []},
    )
    with pytest.raises(BusinessException):
        context.write("system.user", 102, "user.update", UPDATE, ("nickname",))
    context.write("system.user", 101, "user.update", UPDATE, ("nickname",))


async def test_source_user_is_in_the_original_complete_plan(system_sessions):
    factory, _ = system_sessions
    async with factory() as db:
        command = await service.prepare_input(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation="dept.create",
            payload=DeptCreate(dept_name="team", status="1", leader="planner"),
        )
        async with transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            work = await service.plan(db, tx, command)
            assert ResourceKey(K.USER, 0, 101) in work.locks.existing
            await tx.lock(work.locks)
            result = await service.apply(db, tx, work)
            assert result["id"]
        await db.rollback()


async def test_low_level_leader_assignment_requires_source_proof(system_sessions):
    factory, plain = system_sessions
    async with plain() as db:
        await db.execute(
            insert(Dept).values(tenant_id=0, dept_id=401, dept_name="team", status="1")
        )
        await db.commit()
    async with factory() as db:
        with pytest.raises(BusinessException) as poisoned:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                key = ResourceKey(K.DEPT, 0, 401)
                await tx.lock(LockPlan(existing=(key,)))
                with pytest.raises(BusinessException) as error:
                    async with tx.aggregate(0):
                        await tx.update_held(
                            key, {"leader": "planner", "leader_user_id": 101}
                        )
                assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        assert poisoned.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        await db.rollback()


async def test_plan_and_apply_share_one_select_budget(system_sessions):
    factory, _ = system_sessions
    async with factory() as db:
        command = await service.prepare_input(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation="user.update",
            target_id=101,
            payload=UserPatch(nickname="budget"),
        )
        with pytest.raises(BusinessException) as poisoned:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                work = await service.plan(db, tx, command)
                await tx.lock(work.locks)
                work.read_budget.max_statements = work.read_budget.statements
                with pytest.raises(BusinessException) as error:
                    await service.apply(db, tx, work)
                assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
        assert poisoned.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()
