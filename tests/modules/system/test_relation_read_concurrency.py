"""Actual PostgreSQL blockers verify both read/revoke arrival orders."""

import asyncio
import json
from time import perf_counter

import pytest
from rich.console import Console
from sqlalchemy import event

from app.core.exceptions import BusinessException
from app.db import authorization_relation_read_owner as reader_module
from app.db import authorization_user_read_owner as user_reader_module
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.system.schemas.relation_form import RelationFormQuery
from app.modules.system.schemas.role import RoleQuery
from app.modules.system.schemas.user import UserQuery
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate,
)
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations
from tests.modules.system.test_relation_source_concurrency import blocked_by


@pytest.mark.parametrize("writer_first", [False, True])
@pytest.mark.parametrize("form", [False, True])
async def test_relation_reader_and_revocation_use_real_barriers(
    system_sessions, monkeypatch, writer_first, form
):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    entered, release = asyncio.Event(), asyncio.Event()
    pids = []
    connection_pids = {}

    def observed(connection, *_args):
        connection_pids[asyncio.current_task()] = (
            connection.connection.dbapi_connection.driver_connection.get_server_pid()
        )

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", observed)
    if writer_first:
        original = system_authorization_candidate.apply

        async def paused(db, tx, work):
            result = await original(db, tx, work)
            pids.append(connection_pids[asyncio.current_task()])
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(system_authorization_candidate, "apply", paused)
    else:
        original = reader_module.read_relation

        async def paused(db, tx, **kwargs):
            result = await original(db, tx, **kwargs)
            pids.append(connection_pids[asyncio.current_task()])
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(reader_module, "read_relation", paused)

    def read():
        return runtime.relation_read(
            tenant=TENANT,
            actor_user_version=1,
            operation="role.form" if form else "role.list",
            query=RelationFormQuery(mode="update", record_id="201")
            if form
            else RoleQuery(),
        )

    def write():
        return runtime.system(
            tenant=TENANT, actor_user_version=1, operation="user.revoke", target_id=101
        )

    first = asyncio.create_task(write() if writer_first else read())
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        second = asyncio.create_task(read() if writer_first else write())
        assert await blocked_by(plain, pids[0])
        assert not second.done()
        release.set()
        result = await asyncio.wait_for(first, 3)
        if writer_first:
            with pytest.raises(BusinessException) as error:
                await asyncio.wait_for(second, 3)
            assert error.value.error_code == "INVALID_CREDENTIALS"
        else:
            assert (result["record"] if form else result["records"][0])[
                "roleId"
            ] == "201"
            await asyncio.wait_for(second, 3)
        Console(soft_wrap=True, markup=False, highlight=False).print(
            json.dumps(
                {
                    "relation_read_barrier": "writer-first"
                    if writer_first
                    else "reader-first",
                    "actual_pg_blocker_observed": True,
                    "operation": "role.form" if form else "role.list",
                }
            )
        )
    finally:
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", observed)
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in (first, second) if t is not None), return_exceptions=True
        )


async def test_ten_active_user_read_io_samples(system_sessions, monkeypatch):
    _, plain = system_sessions
    await activate_relations(system_sessions, monkeypatch)
    runtime = AuthorizationRuntime(plain.kw["bind"])
    budgets = []
    original = user_reader_module.AuthorizationReadBudget

    def capture(**kwargs):
        budget = original(**kwargs)
        budgets.append(budget)
        return budget

    monkeypatch.setattr(user_reader_module, "AuthorizationReadBudget", capture)
    durations = []
    for _ in range(10):
        start = perf_counter()
        result = await runtime.user_read(
            tenant=TENANT, actor_user_version=1, operation="list", query=UserQuery()
        )
        durations.append(round((perf_counter() - start) * 1000, 3))
        assert result["records"][0]["userId"] == "101"
    assert len(budgets) == 10
    assert all(
        0 < b.statements <= 4096
        and b.rows <= 100000
        and b.decoded_bytes <= 64 * 1024 * 1024
        for b in budgets
    )
    Console(soft_wrap=True, markup=False, highlight=False).print(
        json.dumps(
            {
                "relation_read_io": "user.list/active-seven/synthetic-qualification",
                "samples": [
                    {
                        "selects": b.statements,
                        "budget_rows": b.rows,
                        "decoded_estimate_bytes": b.decoded_bytes,
                        "elapsed_ms": ms,
                    }
                    for b, ms in zip(budgets, durations, strict=True)
                ],
                "dataset": {"users": 1, "roles": 1},
                "includes_commit_ack": False,
            }
        )
    )
