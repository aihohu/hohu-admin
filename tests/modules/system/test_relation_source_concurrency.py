"""Actual PostgreSQL row blockers verify both source/write lock arrival orders."""

import asyncio

import pytest
from sqlalchemy import event, insert, select, text

from app.core.authorization_plan import LockMode, TenantLock
from app.modules.system.models.dept import Dept
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import DeptCreate
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate as service,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from tests.db.lock_observation import wait_for_blocker
from tests.modules.system.test_authorization_system_candidate import TENANT, run
from tests.modules.system.test_relation_field_writes import (
    plan_sessions as _plan_sessions,
)
from tests.modules.system.test_relation_field_writes import (
    system_sessions as _system_sessions,
)

plan_sessions = _plan_sessions
system_sessions = _system_sessions


@pytest.mark.parametrize(
    "source_first, early_snapshot", [(False, False), (True, False), (True, True)]
)
async def test_verified_source_is_in_complete_plan_in_both_orders(
    system_sessions, source_first, early_snapshot
):
    factory, plain = system_sessions
    async with plain() as setup:
        await setup.execute(
            insert(User).values(
                tenant_id=0,
                user_id=103,
                user_name="team-leader",
                hashed_password="not-a-credential",
                status="1",
            )
        )
        await setup.commit()
    payload = DeptCreate(dept_name="source-lock", status="1", leader="team-leader")
    leader_lock = select(User.user_id).where(User.user_id == 103).with_for_update()
    dispatched = asyncio.get_running_loop().create_future()

    def capture(conn, _cursor, sql, params, _context, _many):
        # Only the plan's ordered user lock batch, before apply or FK checks.
        if (
            not dispatched.done()
            and "FROM sys_user " in " ".join(sql.split())
            and "ORDER BY" in sql
            and "FOR UPDATE" in sql
            and 103 in params
        ):
            dispatched.set_result(conn.connection.driver_connection.get_server_pid())

    engine = plain.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        async with plain() as locker, plain() as probe, asyncio.TaskGroup() as tasks:
            locker_pid = await locker.scalar(text("SELECT pg_backend_pid()"))
            await probe.connection()
            if source_first:
                await locker.execute(leader_lock)
                if early_snapshot:
                    # A transaction that has already read pg_stat_activity may
                    # not see a backend that connects after that snapshot.
                    await probe.scalar(text("SELECT count(*) FROM pg_stat_activity"))

                async def write():
                    async with factory() as db:
                        result = await run(db, "dept.create", payload)
                        await db.commit()
                        return result

                writer = tasks.create_task(write())
                try:
                    async with asyncio.timeout(10):
                        done, _ = await asyncio.wait(
                            (writer, dispatched), return_when=asyncio.FIRST_COMPLETED
                        )
                        if writer in done:
                            await writer
                            pytest.fail(
                                "Writer finished before the leader lock was observed"
                            )
                    await wait_for_blocker(
                        probe,
                        holder=locker_pid,
                        waiter=dispatched.result(),
                        pending=writer,
                    )
                finally:
                    await locker.rollback()
                result = await asyncio.wait_for(writer, 10)
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
                        assert dispatched.done(), "Plan did not lock the leader row"
                        waiter = tasks.create_task(locker.execute(leader_lock))
                        await wait_for_blocker(
                            probe,
                            holder=dispatched.result(),
                            waiter=locker_pid,
                            pending=waiter,
                        )
                        result = await service.apply(db, tx, work)
                    await db.commit()
                    assert (await asyncio.wait_for(waiter, 5)).scalar_one() == 103
        async with plain() as db:
            dept = await db.get(Dept, result["id"])
            assert (dept.leader, dept.leader_user_id) == ("team-leader", 103)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
