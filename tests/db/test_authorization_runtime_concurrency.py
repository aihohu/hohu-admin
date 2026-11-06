"""Real independent PostgreSQL connections through the fixed composition root."""

import asyncio

import pytest
from sqlalchemy import event, func, insert, select

from app.core.exceptions import BusinessException
from app.core.tenant import TenantContext
from app.db import authorization_control_owner, authorization_system_owner
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.base import role_menus, user_roles
from app.modules.ai.agents.gateway import authorization_execution
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.role import RoleUpdate
from app.modules.system.schemas.user import UpdateProfile, UserUpdate
from tests.modules.system.test_authorization_system_candidate import (
    TENANT,
    epoch,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


class CommitGate:
    """Pause only the named owner after real DML, before its actual COMMIT."""

    def __init__(self, monkeypatch, engine, *, pause_sql=None):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.second_sql = asyncio.Event()
        self.pids = {}
        self.engine = engine
        self.pause_sql = pause_sql
        self.business_seen = False
        original = authorization_system_owner.commit_authorization_session

        async def commit(db, *args, **kwargs):
            if asyncio.current_task().get_name() == "first-owner" and (
                self.pause_sql is None or self.business_seen
            ):
                self.entered.set()
                await self.release.wait()
            return await original(db, *args, **kwargs)

        for module in (
            authorization_system_owner,
            authorization_control_owner,
            authorization_execution,
        ):
            monkeypatch.setattr(module, "commit_authorization_session", commit)
        event.listen(engine.sync_engine, "before_cursor_execute", self.observe)

    def observe(self, conn, _cursor, sql, _params, _context, _many):
        name = asyncio.current_task().get_name()
        if name in {"first-owner", "second-owner"}:
            self.pids[name] = conn.connection.driver_connection.get_server_pid()
            if name == "first-owner" and self.pause_sql and self.pause_sql in sql:
                self.business_seen = True
            if (
                name == "second-owner"
                and "sys_user_field_policy_" in sql
                and ("FOR UPDATE" in sql or "FOR SHARE" in sql)
            ):
                self.second_sql.set()

    async def assert_blocked(self, plain):
        await asyncio.wait_for(self.second_sql.wait(), 1.5)
        async with plain() as db, asyncio.timeout(1.5):
            while True:
                blockers = await db.scalar(
                    select(func.pg_blocking_pids(self.pids["second-owner"]))
                )
                if self.pids["first-owner"] in blockers:
                    return
                # Yield between observations; the assertion uses PostgreSQL state.
                await asyncio.sleep(0)

    def close(self):
        self.release.set()
        event.remove(self.engine.sync_engine, "before_cursor_execute", self.observe)


@pytest.mark.parametrize("revocation", ["user", "role"])
@pytest.mark.parametrize("revoke_first", [False, True])
async def test_revocation_and_system_write_have_both_serial_orders(
    system_sessions, monkeypatch, revocation, revoke_first
):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    async with plain() as setup:
        await setup.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=303,
                role_name="Editor",
                role_code="EDITOR",
                status="1",
                data_scope="1",
            )
        )
        await setup.execute(
            insert(User),
            [
                {
                    "tenant_id": 0,
                    "user_id": identifier,
                    "user_name": name,
                    "hashed_password": "unused",
                    "status": "1",
                }
                for identifier, name in ((103, "editor"), (104, "target"))
            ],
        )
        await setup.execute(
            insert(user_roles).values(tenant_id=0, user_id=103, role_id=303)
        )
        menu_id = await setup.scalar(
            select(Menu.menu_id).where(
                Menu.permission == "system:user:edit", Menu.tenant_id == 0
            )
        )
        await setup.execute(
            insert(role_menus).values(tenant_id=0, role_id=303, menu_id=menu_id)
        )
        await setup.commit()

    async def revoke():
        return await runtime.system(
            tenant=TENANT,
            actor_user_version=1,
            operation="user.update" if revocation == "user" else "role.update",
            target_id=103 if revocation == "user" else 303,
            payload=UserUpdate(user_name="editor", status="2")
            if revocation == "user"
            else RoleUpdate(status="2"),
        )

    async def write():
        return await runtime.system(
            tenant=TenantContext(0, "plan-0", 103, 1, "access_token"),
            actor_user_version=1,
            operation="user.update",
            target_id=104,
            payload=UserUpdate(user_name="target", status="1", nickname="authorized"),
        )

    gate = CommitGate(monkeypatch, plain.kw["bind"])
    tasks = []
    try:
        tasks.append(
            asyncio.create_task(
                revoke() if revoke_first else write(), name="first-owner"
            )
        )
        await asyncio.wait_for(gate.entered.wait(), 1.5)
        tasks.append(
            asyncio.create_task(
                write() if revoke_first else revoke(), name="second-owner"
            )
        )
        await gate.assert_blocked(plain)
        assert not tasks[1].done()
        gate.release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(results[0], dict), results[0]
        if revoke_first:
            assert isinstance(results[1], BusinessException), results[1]
            assert results[1].code in {401, 403}
        else:
            assert isinstance(results[1], dict), results[1]
        async with plain() as db:
            assert await db.scalar(
                select(User.nickname).where(User.user_id == 104)
            ) == (None if revoke_first else "authorized")
            assert await epoch(db) == (2 if revoke_first else 3)
    finally:
        gate.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_unrelated_tenant_can_commit_while_first_tenant_is_held(
    system_sessions, monkeypatch
):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    gate = CommitGate(monkeypatch, plain.kw["bind"])
    task = asyncio.create_task(
        runtime.system(
            tenant=TENANT,
            actor_user_version=1,
            operation="user.profile",
            payload=UpdateProfile(nickname="first"),
        ),
        name="first-owner",
    )
    try:
        await asyncio.wait_for(gate.entered.wait(), 1.5)
        async with asyncio.timeout(1.5):
            await runtime.system(
                tenant=TenantContext(1, "plan-1", 102, 1, "access_token"),
                actor_user_version=1,
                operation="user.profile",
                payload=UpdateProfile(nickname="parallel"),
            )
        assert not task.done()
        async with plain() as db:
            assert (
                await db.scalar(select(User.nickname).where(User.user_id == 102))
                == "parallel"
            )
        gate.release.set()
        await task
    finally:
        gate.close()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_waiter_leaves_no_write_or_epoch(system_sessions, monkeypatch):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    gate = CommitGate(monkeypatch, plain.kw["bind"])
    tasks = []
    try:
        tasks.append(
            asyncio.create_task(
                runtime.system(
                    tenant=TENANT,
                    actor_user_version=1,
                    operation="user.profile",
                    payload=UpdateProfile(nickname="first"),
                ),
                name="first-owner",
            )
        )
        await asyncio.wait_for(gate.entered.wait(), 1.5)
        tasks.append(
            asyncio.create_task(
                runtime.system(
                    tenant=TENANT,
                    actor_user_version=1,
                    operation="user.profile",
                    payload=UpdateProfile(nickname="cancelled"),
                ),
                name="second-owner",
            )
        )
        await gate.assert_blocked(plain)
        tasks[1].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[1]
        gate.release.set()
        await tasks[0]
        async with plain() as db:
            assert (
                await db.scalar(select(User.nickname).where(User.user_id == 101))
                == "first"
            )
            assert await epoch(db) == 2
        # A fresh owner proves cancelled wait/transaction locks were released.
        await runtime.system(
            tenant=TENANT,
            actor_user_version=1,
            operation="user.profile",
            payload=UpdateProfile(nickname="after"),
        )
        async with plain() as db:
            assert await epoch(db) == 3
    finally:
        gate.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
