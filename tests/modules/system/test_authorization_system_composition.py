"""System mutations compose with AI locks in a single owner transaction."""

import pytest
from sqlalchemy import select

from app.core.authorization_plan import LockMode, LockPlan, TenantLock
from app.core.exceptions import BusinessException
from app.modules.ai.models.conversation import AiConversation
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import DeptCreate
from app.modules.system.schemas.user import UpdateProfile
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate as candidate,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from tests.modules.system.test_authorization_ai_facts import (
    C,
)
from tests.modules.system.test_authorization_ai_facts import (
    ai_sessions as ai_sessions,
)
from tests.modules.system.test_authorization_ai_facts import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_ai_facts import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_candidate import TENANT, epoch


async def prepare(db):
    return await candidate.prepare_input(
        db,
        tenant=TENANT,
        actor_user_version=1,
        operation="user.profile",
        payload=UpdateProfile(nickname="composed"),
    )


async def test_context_and_system_share_one_atomic_transaction(ai_sessions):
    factory, plain = ai_sessions
    async with factory() as db:
        command = await prepare(db)
        async with authorization_transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            work = await candidate.plan(db, tx, command)
            await tx.lock(
                LockPlan(existing=(*work.locks.existing, C), new=work.locks.new)
            )
            await candidate.apply(db, tx, work)
            await tx.update_factual(
                C, {"title": "composed"}, expected={"title": "before"}
            )
        await db.commit()
    async with plain() as db:
        assert (
            await db.scalar(select(User.nickname).where(User.user_id == 101))
            == "composed"
        )
        assert await db.scalar(select(AiConversation.title)) == "composed"
        assert await epoch(db) == 2


@pytest.mark.parametrize("failure", ["missing", "repeat", "late_plan"])
async def test_composed_work_cannot_bypass_complete_plan_or_replay(
    ai_sessions, failure
):
    factory, plain = ai_sessions
    async with factory() as db:
        command = await prepare(db)
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                work = await candidate.plan(db, tx, command)
                await tx.lock(LockPlan() if failure == "missing" else work.locks)
                if failure == "late_plan":
                    await candidate.plan(db, tx, command)
                else:
                    await candidate.apply(db, tx, work)
                    await candidate.apply(db, tx, work)
        await db.rollback()
    async with plain() as db:
        assert await db.scalar(select(User.nickname).where(User.user_id == 101)) is None
        assert await epoch(db) == 1


async def test_prepared_work_is_bound_to_original_transaction(ai_sessions):
    factory, _ = ai_sessions
    async with factory() as db:
        command = await prepare(db)
        async with authorization_transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            work = await candidate.plan(db, tx, command)
            await tx.lock(work.locks)
        await db.rollback()
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as other:
                await other.lock(work.locks)
                await candidate.apply(db, other, work)


async def test_new_resource_reservations_cannot_be_omitted(ai_sessions):
    factory, _ = ai_sessions
    async with factory() as db:
        command = await candidate.prepare_input(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation="dept.create",
            payload=DeptCreate(dept_name="new department", status="1"),
        )
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                work = await candidate.plan(db, tx, command)
                assert work.locks.new
                await tx.lock(LockPlan(existing=work.locks.existing))
                await candidate.apply(db, tx, work)


async def test_mutation_plan_rejects_shared_tenant_barrier(ai_sessions):
    factory, _ = ai_sessions
    async with factory() as db:
        command = await prepare(db)
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                await candidate.plan(db, tx, command)
