"""Real TCP loss on named transaction boundaries with independent DB reads."""

import asyncio
import hashlib
import json
import os
import signal
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.db.session import AsyncSessionLocal, engine, is_session_quarantined
from app.modules.ai.agents.gateway import atomic_execution
from app.modules.ai.api import confirm as confirm_module
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.confirm import ConfirmRequest
from app.modules.auth.service import load_live_user_authority
from tests.modules.ai.pg_fault_proxy import CommitFault, PgFaultProxy
from tests.modules.ai.test_atomic_prepared_completion import (
    assert_success,
    count_real_handler,
    observe,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_completion_recovery_transaction import running_claim


@asynccontextmanager
async def fault_sessions(case, monkeypatch, plan, *, match=None):
    async with PgFaultProxy(engine.url.host, engine.url.port or 5432) as proxy:
        proxied = create_async_engine(
            engine.url.set(host="127.0.0.1", port=proxy.listen_port),
            poolclass=NullPool,
            connect_args={"ssl": False},
        )
        factory = async_sessionmaker(proxied, expire_on_commit=False, autoflush=False)
        monkeypatch.setattr(atomic_execution, "AsyncSessionLocal", factory)
        faults = [CommitFault(stage, mode) for stage, mode in plan]
        remaining = list(faults)

        def locate_commit(session):
            if session.bind is not proxied.sync_engine or not remaining:
                return
            if match is not None:
                if match(session):
                    pid = session.scalar(select(func.pg_backend_pid()))
                    proxy.arm(pid, remaining.pop(0))
                return
            action = next(
                (
                    row
                    for row in session.identity_map.values()
                    if isinstance(row, AiPreparedAction)
                    and row.action_id == case.action_id
                ),
                None,
            )
            if action is not None and action.status == remaining[0].stage:
                pid = session.scalar(select(func.pg_backend_pid()))
                proxy.arm(pid, remaining.pop(0))

        event.listen(Session, "before_commit", locate_commit)
        try:
            yield proxy, factory, faults
        finally:
            event.remove(Session, "before_commit", locate_commit)
            await proxied.dispose()


async def approve_via(case, factory):
    db = factory()
    try:
        actor = await load_live_user_authority(db, tenant=case.tenant)
        return await confirm_module.confirm_tool(
            ConfirmRequest(confirmation_id=case.confirmation_id, action="approve"),
            db=db,
            current_user=actor,
        )
    finally:
        # Match get_db: a detached COMMIT still owns a quarantined session.
        if not is_session_quarantined(db):
            await db.close()


def source_fingerprint():
    root = Path(__file__).resolve().parents[3]
    paths = [
        "app/modules/ai/api/confirm.py",
        "app/modules/ai/agents/gateway/atomic_execution.py",
        "app/modules/ai/agents/gateway/preview_execution.py",
        "app/modules/ai/agents/gateway/preview_handoff.py",
    ]
    return {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths
    }


async def acceptance_context(case):
    async with AsyncSessionLocal() as db:
        primary = dict(
            (
                await db.execute(
                    text(
                        "SELECT current_database() AS database, pg_is_in_recovery() AS standby, "
                        "system_identifier::text FROM pg_control_system()"
                    )
                )
            )
            .mappings()
            .one()
        )
        actions = list(
            await db.scalars(
                select(AiPreparedAction)
                .where(
                    AiPreparedAction.tenant_id == case.tenant.tenant_id,
                    AiPreparedAction.conversation_id == case.conversation_id,
                )
                .order_by(AiPreparedAction.action_id)
            )
        )
        logs = list(
            await db.scalars(
                select(AiOperationLog)
                .where(
                    AiOperationLog.tenant_id == case.tenant.tenant_id,
                    AiOperationLog.conversation_id == case.conversation_id,
                )
                .order_by(AiOperationLog.log_id)
            )
        )
    return {
        "tenant_id": str(case.tenant.tenant_id),
        "actor_id": str(case.tenant.actor_user_id),
        "conversation_id": str(case.conversation_id),
        "primary": primary,
        "source_files": await asyncio.to_thread(source_fingerprint),
        "ci_sha": os.environ.get("GITHUB_SHA"),
        "actions": [
            {
                "action_id": str(a.action_id),
                "confirmation_id": a.confirmation_id,
                "tool_call_id": a.execute_tool_call_id,
                "status": a.status,
            }
            for a in actions
        ],
        "logs": [
            {
                "log_id": str(row.log_id),
                "tool_call_id": row.tool_call_id,
                "trace_id": row.trace_id,
                "tool": row.tool_name,
                "status": row.status,
            }
            for row in logs
        ],
    }


async def record(tmp_path, case, faults, state, calls, elapsed):
    (tmp_path / "transport-evidence.json").write_text(
        json.dumps(
            {
                "context": await acceptance_context(case),
                "faults": [
                    {"stage": f.stage, "mode": f.mode, "events": f.events}
                    for f in faults
                ],
                "handler_calls": len(calls),
                "handler_transactions": calls,
                "user_count": len(state["user_ids"]),
                "action_status": state["action_status"],
                "operation_status": state["operation_status"],
                "elapsed_seconds": elapsed,
            },
            indent=2,
        ),
        encoding="utf8",
    )


@pytest.mark.parametrize(
    ("plan", "reject_result", "expected", "calls"),
    [
        ([("running", "drop_before")], False, "pending_confirmation", 0),
        ([("running", "drop_after")], False, "failed", 0),
        ([("succeeded", "drop_before")], False, "failed", 1),
        ([("succeeded", "drop_after")], False, "succeeded", 1),
        ([("failed", "drop_before")], True, "failed", 1),
        ([("failed", "drop_after")], True, "failed", 1),
        (
            [("succeeded", "drop_before"), ("failed", "drop_after")],
            False,
            "failed",
            1,
        ),
        (
            [("succeeded", "drop_before"), ("failed", "drop_before")],
            False,
            "running",
            1,
        ),
    ],
)
async def test_real_commit_loss_branches(
    completion_case, monkeypatch, tmp_path, plan, reject_result, expected, calls
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=reject_result)
    async with fault_sessions(case, monkeypatch, plan) as (_, factory, faults):
        started = time.monotonic()
        response = await asyncio.wait_for(approve_via(case, factory), 24)
        elapsed = time.monotonic() - started
        assert all(f.reached.is_set() for f in faults)
        state = await observe(case)
        await record(tmp_path, case, faults, state, case.calls, elapsed)
        assert state["action_status"] == expected
        assert len(case.calls) == calls
        if expected == "succeeded":
            assert_success(case, state)
        else:
            assert state["user_ids"] == []
            assert state["role_ids"] == []
            assert state["department_rows"] == []
        if expected in {"succeeded", "failed"}:
            assert response.data.status == expected
            assert (await approve_via(case, factory)).data.status == expected
            assert len(case.calls) == calls
        else:
            assert response.data.status == "running"


@pytest.mark.parametrize("stage", ["running", "succeeded", "failed"])
async def test_silent_commit_ack_loss_has_bounded_response(
    completion_case, monkeypatch, tmp_path, stage
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=stage == "failed")
    async with fault_sessions(case, monkeypatch, [(stage, "hold_after")]) as (
        _,
        factory,
        faults,
    ):
        started = time.monotonic()
        request = asyncio.create_task(approve_via(case, factory))
        try:
            await asyncio.wait_for(faults[0].reached.wait(), 8)
            # COMMIT succeeded on PostgreSQL. The application has received
            # neither CommandComplete nor EOF, so only its own budget can end it.
            state = await observe(case)
            assert state["action_status"] == stage
            if stage == "succeeded":
                assert_success(case, state)
            else:
                assert state["user_ids"] == []
            done, _ = await asyncio.wait({request}, timeout=12)
            assert request in done, "COMMIT has no bounded response on silent ACK loss"
            assert request.result().data.status == (
                "succeeded" if stage == "succeeded" else "failed"
            )
            await record(
                tmp_path,
                case,
                faults,
                await observe(case),
                case.calls,
                time.monotonic() - started,
            )
        finally:
            faults[0].release.set()
            done, _ = await asyncio.wait({request}, timeout=5)
            if request not in done:
                request.cancel()
            await asyncio.wait({request}, timeout=5)


async def process_run(case, tmp_path, mode, *, proxy=None, fault=None, reject=False):
    request = {
        "mode": mode,
        "tenant_id": case.tenant.tenant_id,
        "actor_id": case.tenant.actor_user_id,
        "action_id": case.action_id,
        "confirmation_id": case.confirmation_id,
        "username": case.username,
        "stage": fault.stage if fault else None,
        "reject_result": reject,
    }
    request_path = tmp_path / f"{mode}-request.json"
    request_path.write_text(json.dumps(request), encoding="utf8")
    environment = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if proxy:
        environment["DATABASE_URL"] = engine.url.set(
            host="127.0.0.1", port=proxy.listen_port
        ).render_as_string(hide_password=False)
    events = []
    with (tmp_path / f"{mode}-stderr.txt").open("wb") as errors:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.modules.ai.completion_process",
            str(request_path),
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=errors,
        )

        async def receive():
            while line := await process.stdout.readline():
                if not line.startswith(b"{"):
                    continue
                value = json.loads(line)
                events.append(value)
                if value["event"] == "arm":
                    proxy.arm(value["pid"], fault)
                    process.stdin.write(b"armed\n")
                    await process.stdin.drain()

        receiver = asyncio.create_task(receive())
        killed = False
        try:
            if fault:
                reached = asyncio.create_task(fault.reached.wait())
                exited = asyncio.create_task(process.wait())
                try:
                    await asyncio.wait(
                        {reached, exited},
                        timeout=30,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    assert reached.done(), (
                        "Worker exited or timed out before COMMIT fault"
                    )
                finally:
                    reached.cancel()
                    exited.cancel()
                    await asyncio.gather(reached, exited, return_exceptions=True)
                worker_pid = next(e["pid"] for e in events if e["event"] == "started")
                # Windows venv redirectors may have a different PID. Terminate
                # only the PID reported over this child's private stdout pipe.
                os.kill(worker_pid, signal.SIGTERM)
                killed = True
                await asyncio.wait_for(process.wait(), 10)
                fault.release.set()
            else:
                assert await asyncio.wait_for(process.wait(), 30) == 0
            await asyncio.wait_for(receiver, 5)
        finally:
            if fault:
                fault.release.set()
            if process.returncode is None:
                process.kill()
                await process.wait()
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
    (tmp_path / f"{mode}-events.json").write_text(
        json.dumps({"killed": killed, "events": events}, indent=2), encoding="utf8"
    )
    return events


@pytest.mark.parametrize("stage", ["running", "succeeded", "failed"])
@pytest.mark.parametrize("committed", [False, True])
async def test_real_process_death_then_cold_recovery(
    completion_case, tmp_path, stage, committed
):
    case = completion_case
    fault = CommitFault(stage, "hold_after" if committed else "hold_before")
    async with PgFaultProxy(engine.url.host, engine.url.port or 5432) as proxy:
        events = await process_run(
            case,
            tmp_path,
            "approve",
            proxy=proxy,
            fault=fault,
            reject=stage == "failed",
        )
    started = [e for e in events if e["event"] == "handler_started"]
    case.calls.extend((e["pid"], e["txid"]) for e in started)
    case.created_user_ids.extend(
        e["user_id"] for e in events if e["event"] == "handler_written"
    )
    assert len(case.calls) == (0 if stage == "running" else 1)
    # Time passage is a controlled DB fixture, not a lease takeover. The old
    # process is dead and all proxy connections have been closed first.
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            update(AiPreparedAction)
            .where(
                AiPreparedAction.action_id == case.action_id,
                AiPreparedAction.status == "running",
            )
            .values(execution_lease_expires_at=func.now() - text("interval '1 second'"))
        )
    recovered = await process_run(case, tmp_path, "recover")
    assert not any(e["event"].startswith("handler_") for e in recovered)
    disposition = next(e["disposition"] for e in recovered if e["event"] == "result")
    expected = (
        "unknown"
        if stage == "running" and not committed
        else "succeeded"
        if stage == "succeeded" and committed
        else "failed"
    )
    assert disposition == expected
    state = await observe(case)
    if expected == "succeeded":
        assert_success(case, state)
    else:
        assert state["user_ids"] == []
        assert state["action_status"] == (
            "pending_confirmation" if expected == "unknown" else "failed"
        )
    await record(tmp_path, case, [fault], state, case.calls, 0)


async def test_live_uncommitted_transaction_keeps_recovery_unknown(
    completion_case, monkeypatch, tmp_path
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    async with fault_sessions(case, monkeypatch, [("succeeded", "hold_before")]) as (
        _,
        factory,
        faults,
    ):
        request = asyncio.create_task(approve_via(case, factory))
        try:
            await asyncio.wait_for(faults[0].reached.wait(), 8)
            started = time.monotonic()
            outcome = await atomic_execution.reconcile_prepared_completion(
                case.confirmation_id, tenant=case.tenant
            )
            assert outcome.disposition == "unknown"
            assert time.monotonic() - started < 4
            state = await observe(case)
            assert state["action_status"] == state["operation_status"] == "running"
            assert state["user_ids"] == [] and len(case.calls) == 1
            await record(
                tmp_path, case, faults, state, case.calls, time.monotonic() - started
            )
        finally:
            faults[0].release.set()
            await asyncio.wait_for(request, 12)
        assert (await observe(case))["action_status"] == "failed"


async def test_server_processing_commit_keeps_recovery_unknown(
    completion_case, monkeypatch, tmp_path
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    # A deferred constraint executes inside server COMMIT, after all application
    # statements. A separate session holds its advisory lock as an event barrier.
    identifier = "ack_gate_" + uuid4().hex
    assert identifier.isascii() and identifier.replace("_", "").isalnum()
    async with engine.connect() as gate:
        await gate.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": case.action_id}
        )
        await gate.execute(
            text(
                "CREATE FUNCTION "
                + identifier
                + "() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN PERFORM set_config('lock_timeout', '10s', true); "
                "PERFORM pg_advisory_xact_lock(NEW.action_id); RETURN NEW; END $$"
            )
        )
        await gate.execute(
            text(
                "CREATE CONSTRAINT TRIGGER "
                + identifier
                + " AFTER UPDATE ON ai_prepared_action DEFERRABLE INITIALLY DEFERRED "
                "FOR EACH ROW WHEN (NEW.status = 'succeeded') EXECUTE FUNCTION "
                + identifier
                + "()"
            )
        )
        await gate.commit()
        try:
            async with fault_sessions(
                case, monkeypatch, [("succeeded", "hold_after")]
            ) as (_, factory, faults):
                request = asyncio.create_task(approve_via(case, factory))
                try:
                    await asyncio.wait_for(faults[0].sent.wait(), 5)
                    pid = case.calls[0][0]
                    async with asyncio.timeout(3):
                        while True:
                            waiting = await gate.scalar(
                                text(
                                    "SELECT wait_event FROM pg_stat_activity WHERE pid = :pid"
                                ),
                                {"pid": pid},
                            )
                            await gate.commit()
                            if waiting == "advisory":
                                break
                            await asyncio.sleep(0.01)
                    faults[0].events.append(
                        "server_commit_waiting_on_deferred_constraint"
                    )
                    assert not faults[0].reached.is_set()
                    started = time.monotonic()
                    outcome = await atomic_execution.reconcile_prepared_completion(
                        case.confirmation_id, tenant=case.tenant
                    )
                    assert outcome.disposition == "unknown"
                    assert time.monotonic() - started < 4
                    state = await observe(case)
                    assert (
                        state["action_status"] == state["operation_status"] == "running"
                    )
                    assert not state["user_ids"]
                    await gate.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": case.action_id}
                    )
                    await gate.commit()
                    await asyncio.wait_for(faults[0].reached.wait(), 3)
                    faults[0].release.set()
                    assert (
                        await asyncio.wait_for(request, 8)
                    ).data.status == "succeeded"
                    state = await observe(case)
                    assert_success(case, state)
                    assert len(case.calls) == 1
                    await record(
                        tmp_path,
                        case,
                        faults,
                        state,
                        case.calls,
                        time.monotonic() - started,
                    )
                finally:
                    await gate.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": case.action_id}
                    )
                    await gate.commit()
                    faults[0].release.set()
                    await asyncio.wait_for(request, 12)
        finally:
            await gate.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": case.action_id}
            )
            await gate.execute(
                text("DROP TRIGGER IF EXISTS " + identifier + " ON ai_prepared_action")
            )
            await gate.execute(text("DROP FUNCTION IF EXISTS " + identifier + "()"))
            await gate.commit()


async def test_committed_result_stays_unknown_when_primary_unreachable(
    completion_case, monkeypatch, tmp_path
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    async with fault_sessions(case, monkeypatch, [("succeeded", "hold_after")]) as (
        proxy,
        factory,
        faults,
    ):
        request = asyncio.create_task(approve_via(case, factory))
        await asyncio.wait_for(faults[0].reached.wait(), 8)
        proxy.unavailable = True
        faults[0].release.set()
        response = await asyncio.wait_for(request, 12)
        assert response.data.status == "running"
        assert_success(case, await observe(case))
        proxy.unavailable = False
        assert (await approve_via(case, factory)).data.status == "succeeded"
        assert len(case.calls) == 1
        await record(tmp_path, case, faults, await observe(case), case.calls, 0)


async def test_readonly_recovery_endpoint_cannot_prove_rollback(
    completion_case, monkeypatch, tmp_path
):
    case = completion_case
    claim = await running_claim(case)
    readonly = create_async_engine(
        engine.url,
        poolclass=NullPool,
        connect_args={"server_settings": {"default_transaction_read_only": "on"}},
    )
    monkeypatch.setattr(
        atomic_execution,
        "AsyncSessionLocal",
        async_sessionmaker(readonly, expire_on_commit=False),
    )
    try:
        outcome = await atomic_execution.reconcile_prepared_completion(
            case.confirmation_id,
            tenant=case.tenant,
            expected_claim=claim,
            allow_fence=True,
        )
        assert outcome.disposition == "unknown"
        state = await observe(case)
        assert state["action_status"] == state["operation_status"] == "running"
        await record(tmp_path, case, [], state, case.calls, 0)
    finally:
        await readonly.dispose()
