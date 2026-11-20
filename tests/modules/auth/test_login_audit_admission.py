"""Real audit admission failures preserve authentication and never retry."""

import asyncio
import time
from contextlib import AsyncExitStack
from contextvars import ContextVar
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.util.concurrency import await_only

from app.db.authorization_http_audit_owner import HttpAuditOwner
from app.modules.system.models.login_log import SysLoginLog
from tests.modules.auth.test_authorization_http import (
    accounts as accounts,
)
from tests.modules.auth.test_authorization_http import (
    auth_http as auth_http,
)
from tests.modules.auth.test_authorization_http import (
    auth_http_engine as auth_http_engine,
)


@pytest.fixture(params=["connection", "global", "tenant", "dispatch"])
async def stalled_login_audit(request, auth_http, monkeypatch):
    _, accounts = auth_http
    engine = accounts.http_engine
    phase = request.param
    active = ContextVar("stalled_login_audit", default=False)
    state = SimpleNamespace(
        calls=0, stages=[], errors=[], blocked=False, reader_pid=None
    )
    original = HttpAuditOwner.login
    waiting = asyncio.Event()

    def connect(*_args):
        if active.get():
            state.stages.append("connection")
            if phase == "connection":
                await_only(asyncio.Event().wait())

    def before(conn, _cursor, sql, _params, _context, _many):
        if not active.get():
            return
        if "sys_user_field_policy_global_guard" in sql:
            stage = "global"
        elif "sys_user_field_policy_state" in sql:
            stage = "tenant"
        elif sql.startswith("INSERT INTO sys_login_log"):
            stage = "insert"
        else:
            stage = "configure"
        state.stages.append(stage)
        if phase in {"global", "tenant"} and stage == phase:
            state.reader_pid = conn.connection.driver_connection.get_server_pid()
            waiting.set()
        if phase == "dispatch" and len(state.stages) == 1:
            # Deterministically exhaust the original 500ms admission budget.
            # This models a paused event loop; it does not guess a race order.
            time.sleep(0.6)

    async def login(owner, **values):
        state.calls += 1
        async with AsyncExitStack() as stack:
            observer = None
            if phase == "connection":
                await engine.dispose()
            if phase in {"global", "tenant"}:
                blocker = await stack.enter_async_context(accounts.plain())
                holder = await blocker.scalar(text("SELECT pg_backend_pid()"))
                statement = (
                    "SELECT guard_id FROM sys_user_field_policy_global_guard "
                    "WHERE guard_id=1 FOR UPDATE"
                    if phase == "global"
                    else "SELECT tenant_id FROM sys_user_field_policy_state "
                    "WHERE tenant_id=0 FOR UPDATE"
                )
                await blocker.execute(text(statement))
                db = await stack.enter_async_context(accounts.plain())
                # Establish observation before the owner's admission clock starts.
                await db.connection()

                async def observe_blocker():
                    await waiting.wait()
                    while not state.blocked:
                        state.blocked = bool(
                            await db.scalar(
                                text("SELECT :holder = ANY(pg_blocking_pids(:reader))"),
                                {"holder": holder, "reader": state.reader_pid},
                            )
                        )
                        if not state.blocked:
                            await asyncio.sleep(0.01)

                observer = asyncio.create_task(observe_blocker())
            token = active.set(True)
            try:
                return await original(owner, **values)
            except Exception as error:
                state.errors.append(getattr(error, "error_code", None))
                raise
            finally:
                active.reset(token)
                if observer is not None:
                    observer.cancel()
                    await asyncio.gather(observer, return_exceptions=True)

    monkeypatch.setattr(HttpAuditOwner, "login", login)
    event.listen(engine.sync_engine, "do_connect", connect)
    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        yield phase, state
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
        assert state.blocked
        assert phase in state.stages
    else:
        assert state.stages == ["configure"]
    assert accounts.http_engine.pool.checkedout() == 0
    async with accounts.plain() as db:
        assert list(await db.scalars(select(SysLoginLog))) == []
        # Both barriers must be reusable immediately after the rejected attempt.
        await db.execute(
            text(
                "SELECT guard_id FROM sys_user_field_policy_global_guard "
                "WHERE guard_id=1 FOR UPDATE NOWAIT"
            )
        )
        await db.execute(
            text(
                "SELECT tenant_id FROM sys_user_field_policy_state "
                "WHERE tenant_id=0 FOR UPDATE NOWAIT"
            )
        )
