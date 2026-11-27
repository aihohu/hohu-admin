"""Real audit admission failures preserve authentication and never retry."""

import asyncio
import time
from contextlib import AsyncExitStack
from contextvars import ContextVar
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.util.concurrency import await_only

from app.db import authorization_http_audit_owner
from app.db.authorization_http_audit_owner import HttpAuditOwner
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.system.models.login_log import SysLoginLog
from tests.db.lock_observation import wait_for_blocker
from tests.modules.auth.test_authorization_http import (
    accounts as accounts,
)
from tests.modules.auth.test_authorization_http import (
    auth_http as auth_http,
)
from tests.modules.auth.test_authorization_http import (
    auth_http_engine as auth_http_engine,
)

BARRIER_LOCKS = {
    "global": "SELECT guard_id FROM sys_user_field_policy_global_guard WHERE guard_id=1 FOR UPDATE",
    "tenant": "SELECT tenant_id FROM sys_user_field_policy_state WHERE tenant_id=0 FOR UPDATE",
}


@pytest.fixture(
    params=["connection", "global", "tenant", "dispatch", "global-configure"]
)
async def stalled_login_audit(request, auth_http, monkeypatch):
    _, accounts = auth_http
    engine = accounts.http_engine
    phase = "global" if request.param == "global-configure" else request.param
    active = ContextVar("stalled_login_audit", default=False)
    state = SimpleNamespace(calls=0, stages=[], errors=[])
    original = HttpAuditOwner.login

    def connect(*_args):
        if active.get():
            state.stages.append("connection")
            if phase == "connection":
                await_only(asyncio.Event().wait())

    def before(_conn, _cursor, sql, _params, _context, _many):
        if not active.get():
            return
        if "sys_user_field_policy_global_guard" in sql:
            stage = "global"
        elif "sys_user_field_policy_state" in sql:
            stage = "tenant"
        elif sql.startswith("INSERT INTO sys_login_log"):
            stage = "insert"
        elif "set_config('lock_timeout'" in sql:
            stage = "configure"
        else:
            stage = "other"
        state.stages.append(stage)
        if request.param in {"dispatch", "global-configure"} and stage == "configure":
            # Deterministically exhaust the original 500ms admission budget.
            # This models a paused event loop; it does not guess a race order.
            time.sleep(0.6)

    async def login(owner, **values):
        state.calls += 1
        async with AsyncExitStack() as stack:
            if phase == "connection":
                await engine.dispose()
            if phase in {"global", "tenant"}:
                blocker = await stack.enter_async_context(accounts.plain())
                await blocker.execute(text(BARRIER_LOCKS[phase]))
            token = active.set(True)
            try:
                return await original(owner, **values)
            except Exception as error:
                state.errors.append(getattr(error, "error_code", None))
                raise
            finally:
                active.reset(token)

    monkeypatch.setattr(HttpAuditOwner, "login", login)
    event.listen(engine.sync_engine, "do_connect", connect)
    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        yield request.param, state
    finally:
        event.remove(engine.sync_engine, "do_connect", connect)
        event.remove(engine.sync_engine, "before_cursor_execute", before)


@pytest.mark.parametrize("accepted", [True, False])
async def test_audit_admission_failure_preserves_authentication_without_retry(
    auth_http, stalled_login_audit, accepted
):
    client, accounts = auth_http
    phase, state = stalled_login_audit
    response = await client.post(
        "/auth/login",
        json={
            "userName": "alice",
            "password": "OldStrong42!" if accepted else "WrongPassword!",
        },
    )
    assert response.status_code == (200 if accepted else 401), response.text
    if accepted:
        assert set(response.json()["data"]) == {"token", "refreshToken"}
    assert state.calls == 1
    assert len(state.errors) == 1
    assert state.errors[0] in {
        "AUTHORIZATION_TRANSACTION_TIMEOUT",
        "FIELD_POLICY_UNAVAILABLE",
    }
    assert "insert" not in state.stages
    if phase == "connection":
        assert state.stages == ["connection"]
    elif phase in {"global", "tenant"}:
        # The holder stays locked throughout admission. The real deadline can
        # expire before its target, but the owner must never advance past it.
        if phase in state.stages:
            assert state.stages[-1] == phase
        else:
            assert state.errors == ["AUTHORIZATION_TRANSACTION_TIMEOUT"]
            assert set(state.stages) <= (
                {"connection", "configure"}
                if phase == "global"
                else {"connection", "configure", "global"}
            )
    else:
        assert state.stages in (["configure"], ["connection", "configure"])
        assert state.errors == ["AUTHORIZATION_TRANSACTION_TIMEOUT"]
    assert accounts.http_engine.pool.checkedout() == 0
    async with accounts.plain() as db:
        assert list(await db.scalars(select(SysLoginLog))) == []
        # Both barriers must be reusable immediately after the rejected attempt.
        for statement in BARRIER_LOCKS.values():
            await db.execute(text(statement + " NOWAIT"))


@pytest.mark.parametrize("phase", ["global", "tenant"])
async def test_login_audit_holds_barrier_until_commit(auth_http, monkeypatch, phase):
    _, accounts = auth_http
    runtime = AuthorizationRuntime(accounts.http_engine)
    original_commit = authorization_http_audit_owner.commit_authorization_session
    locked = []

    def capture(conn, _cursor, sql, _params, _context, _many):
        if "FOR SHARE" in sql:
            for stage, table in (
                ("global", "sys_user_field_policy_global_guard"),
                ("tenant", "sys_user_field_policy_state"),
            ):
                if table in sql:
                    locked.append(
                        (stage, conn.connection.driver_connection.get_server_pid())
                    )

    async with accounts.plain() as contender, accounts.plain() as probe:
        pid = await contender.scalar(text("SELECT pg_backend_pid()"))
        await probe.connection()
        # Exercise cold connection admission in the failure fixture. Prepare the
        # pooled owner connection before the independent lock-retention proof.
        async with accounts.http_engine.connect() as warm:
            await warm.execute(text("SELECT 1"))
        confirmed = False

        async def commit(db):
            nonlocal confirmed
            assert [stage for stage, _ in locked] == ["global", "tenant"]
            holder = locked[0][1]
            assert locked[1][1] == holder
            # The protected phase has closed; ACK starts at original_commit.
            # Both real row locks must still be held here, however long setup took.
            async with asyncio.TaskGroup() as tasks:
                pending = tasks.create_task(
                    contender.execute(text(BARRIER_LOCKS[phase]))
                )
                await wait_for_blocker(
                    probe, holder=holder, waiter=pid, pending=pending
                )
                await original_commit(db)
                assert (await asyncio.wait_for(pending, 5)).scalar_one() == (
                    1 if phase == "global" else 0
                )
                confirmed = True

        monkeypatch.setattr(
            authorization_http_audit_owner, "commit_authorization_session", commit
        )
        engine = accounts.http_engine.sync_engine
        event.listen(engine, "after_cursor_execute", capture)
        try:
            await runtime.http_audit.login(
                tenant_id=0,
                audit_scope="tenant",
                user_id=101,
                username="alice",
                ip=None,
                user_agent=None,
                status="1",
                message="success",
            )
        finally:
            event.remove(engine, "after_cursor_execute", capture)
        assert confirmed
        assert (await probe.scalars(select(SysLoginLog.username))).all() == ["alice"]
