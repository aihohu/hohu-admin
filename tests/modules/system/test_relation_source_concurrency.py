"""Actual PostgreSQL row blockers verify both source/write lock arrival orders."""

import asyncio

import pytest
from sqlalchemy import select, text

from app.core.authorization_plan import LockMode, TenantLock
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import DeptCreate
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate as service,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from tests.modules.system.test_authorization_system_candidate import TENANT, run
from tests.modules.system.test_relation_field_writes import (
    plan_sessions as _plan_sessions,
)
from tests.modules.system.test_relation_field_writes import (
    system_sessions as _system_sessions,
)

plan_sessions = _plan_sessions
system_sessions = _system_sessions


async def blocked_by(plain, blocker):
    async with plain() as probe:
        for _ in range(30):
            waiting = await probe.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                    "AND :blocker=ANY(pg_blocking_pids(pid)))"
                ),
                {"blocker": blocker},
            )
            if waiting:
                return True
            await asyncio.sleep(0.02)
    return False


@pytest.mark.parametrize("source_first", [False, True])
async def test_verified_source_is_in_complete_plan_in_both_orders(
    system_sessions, source_first
):
    factory, plain = system_sessions
    payload = DeptCreate(dept_name="source-lock", status="1", leader="planner")
    async with plain() as locker:
        pid = await locker.scalar(text("SELECT pg_backend_pid()"))
        if source_first:
            await locker.execute(
                select(User.user_id).where(User.user_id == 101).with_for_update()
            )

            async def write():
                async with factory() as db:
                    result = await run(db, "dept.create", payload)
                    await db.commit()
                    return result

            writer = asyncio.create_task(write())
            try:
                assert await blocked_by(plain, pid)
                assert not writer.done()
            finally:
                await locker.rollback()
            assert (await asyncio.wait_for(writer, 3))["id"]
        else:
            async with factory() as db:
                command = await service.prepare_input(
                    db,
                    tenant=TENANT,
                    actor_user_version=1,
                    operation="dept.create",
                    payload=payload,
                )
                async with transactions.scope(
                    db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
                ) as tx:
                    work = await service.plan(db, tx, command)
                    await tx.lock(work.locks)
                    waiter = asyncio.create_task(
                        locker.execute(
                            select(User.user_id)
                            .where(User.user_id == 101)
                            .with_for_update()
                        )
                    )
                    try:
                        async with plain() as probe:
                            blockers = await probe.scalar(
                                text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}
                            )
                            for _ in range(30):
                                if blockers:
                                    break
                                await asyncio.sleep(0.02)
                                blockers = await probe.scalar(
                                    text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}
                                )
                            assert blockers
                        assert not waiter.done()
                        assert (await service.apply(db, tx, work))["id"]
                    finally:
                        # The owner ends its scope before releasing domain locks.
                        pass
                await db.rollback()
                await asyncio.wait_for(waiter, 3)
                await locker.rollback()
