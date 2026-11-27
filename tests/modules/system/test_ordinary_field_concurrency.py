"""PostgreSQL attests both orders of a read and a current-role revocation."""

import asyncio
from contextvars import ContextVar

import pytest
from sqlalchemy import event, text

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.exceptions import BusinessException
from app.db import authorization_business_owner as owners
from app.modules.system.schemas.dict_type import DictTypeQuery
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from tests.modules.system.test_authorization_business_owner import business as business
from tests.modules.system.test_authorization_business_owner import make_type
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.mark.parametrize("first", ["reader", "writer"])
async def test_role_revocation_and_read_share_real_barrier(
    business, system_sessions, monkeypatch, first
):
    execute, plain, owner = business
    factory, _ = system_sessions
    await make_type(execute)
    label = ContextVar("ordinary_lock_test", default=None)
    pids, started, held, release = {}, asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = owners.read_ordinary

    async def read(*args, **kwargs):
        result = await original(*args, **kwargs)
        if first == "reader":
            held.set()
            await release.wait()
        return result

    monkeypatch.setattr(owners, "read_ordinary", read)

    def observe(conn, _cursor, sql, _params, _context, _many):
        current = label.get()
        if current:
            pids[current] = conn.connection.driver_connection.get_server_pid()
            if (
                current != first
                and "sys_user_field_policy_state" in sql
                and "FOR " in sql
            ):
                started.set()

    async def reader():
        label.set("reader")
        return await owner.read(
            tenant=TENANT,
            actor_user_version=1,
            operation="dict-type.list",
            query=DictTypeQuery(),
        )

    async def writer():
        label.set("writer")
        async with factory() as db:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                key = ResourceKey(ResourceKind.ROLE, 0, 201)
                await tx.lock(LockPlan(existing=(key,)))
                async with tx.aggregate(0):
                    await tx.update_held(key, {"status": "2"})
                if first == "writer":
                    held.set()
                    await release.wait()
            await db.commit()

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", observe)
    tasks = {}
    try:
        tasks[first] = asyncio.create_task(reader() if first == "reader" else writer())
        tasks[first].add_done_callback(lambda _: held.set())
        await asyncio.wait_for(held.wait(), 3)
        if tasks[first].done():
            await tasks[first]
            pytest.fail("holder released before synchronization")
        second = "writer" if first == "reader" else "reader"
        tasks[second] = asyncio.create_task(
            writer() if second == "writer" else reader()
        )
        await asyncio.wait_for(started.wait(), 3)
        async with plain() as observer, asyncio.timeout(1):
            while pids[first] not in await observer.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": pids[second]}
            ):
                assert not tasks[second].done()
        release.set()
        await asyncio.wait_for(tasks["writer"], 3)
        if first == "writer":
            with pytest.raises(BusinessException):
                await tasks["reader"]
        else:
            assert (await tasks["reader"])["total"] == 1
        with pytest.raises(BusinessException):
            await reader()
    finally:
        release.set()
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", observe)
