"""Real recovery transactions with injected client-side commit/cleanup failures.

These exercise phase classification, not a real network ACK loss or failover.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db import session as session_module
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai.agents.gateway import atomic_execution
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.atomic_completion_service import (
    ClaimBinding,
    atomic_completion_service,
    new_execution_owner,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)


async def running_claim(case):
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        action.status = log.status = "running"
        action.row_version = 3
        action.execution_owner = new_execution_owner(action.execute_tool_name)
        action.execution_lease_expires_at = now + timedelta(minutes=1)
        action.approved_by = log.approved_by = case.tenant.actor_user_id
        action.approved_at = now
        log.started_at = now.replace(tzinfo=None)
        await db.flush()
        return ClaimBinding.from_action(action, log.log_id)


async def facts(case):
    async with AsyncSessionLocal() as db:
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        return action.status, action.row_version, action.execution_owner, log.status


async def test_partial_failure_is_not_committed_as_unknown(
    completion_case, monkeypatch
):
    case = completion_case
    claim = await running_claim(case)
    monkeypatch.setattr(
        atomic_completion_service,
        "_finish_log",
        AsyncMock(side_effect=RuntimeError("required log write unavailable")),
    )
    result = await atomic_execution.reconcile_prepared_completion(
        case.confirmation_id,
        tenant=case.tenant,
        expected_claim=claim,
        allow_fence=True,
    )
    assert result.disposition == "unknown"
    assert await facts(case) == ("running", 3, claim.execution_owner, "running")


@pytest.mark.parametrize(
    "committed,cancelled", [(False, False), (True, False), (False, True), (True, True)]
)
async def test_commit_phase_failure_does_not_repeat_fencing(
    completion_case, monkeypatch, committed, cancelled
):
    case = completion_case
    claim = await running_claim(case)
    first = True
    attempts = []

    def session_factory():
        nonlocal first
        db = AsyncSessionLocal()
        if first:
            first = False
            original_commit = db.commit
            original_close = db.close

            async def tracked_close():
                attempts.append("close")
                await original_close()

            async def fault_commit():
                attempts.append("commit")
                if committed:
                    await original_commit()
                else:
                    await db.rollback()
                if cancelled:
                    raise asyncio.CancelledError()
                raise ConnectionError("client lost recovery commit acknowledgement")

            db.commit = fault_commit
            db.close = tracked_close
        return db

    monkeypatch.setattr(atomic_execution, "AsyncSessionLocal", session_factory)
    result = await atomic_execution.reconcile_prepared_completion(
        case.confirmation_id,
        tenant=case.tenant,
        expected_claim=claim,
        allow_fence=True,
    )
    assert attempts == ["commit"]
    assert result.disposition == (
        "failed" if committed and not cancelled else "unknown"
    )
    assert await facts(case) == (
        "failed" if committed else "running",
        4 if committed else 3,
        claim.execution_owner,
        "failed" if committed else "running",
    )


async def test_cleanup_cancellation_cannot_extend_recovery_beyond_http_budget(
    completion_case, monkeypatch
):
    case = completion_case
    await running_claim(case)
    cleanup_release = asyncio.Event()
    never_close = asyncio.Event()
    sessions = []

    def session_factory():
        db = AsyncSessionLocal()
        original_close, original_invalidate = db.close, db.invalidate
        sessions.append((db, original_close))

        async def blocked_close():
            await never_close.wait()
            await original_close()

        async def blocked_invalidate():
            await cleanup_release.wait()
            await original_invalidate()

        db.close = blocked_close
        db.invalidate = blocked_invalidate
        return db

    async def harness_release():
        # A bounded test harness releases the simulated stuck network operation;
        # the product must already have returned within its <=10s HTTP budget.
        await asyncio.sleep(11)
        cleanup_release.set()

    monkeypatch.setattr(atomic_execution, "AsyncSessionLocal", session_factory)
    release_task = asyncio.create_task(harness_release())
    checked_out_before = engine.pool.checkedout()
    started = time.monotonic()
    try:
        result = await atomic_execution.reconcile_prepared_completion(
            case.confirmation_id,
            tenant=case.tenant,
        )
        elapsed = time.monotonic() - started
        assert result.disposition == "unknown"
        assert elapsed <= 10, (
            f"Recovery consumed {elapsed:.2f}s after cleanup cancelled"
        )
        assert any(
            db is sessions[0][0] and not task.done()
            for db, task in atomic_execution._QUARANTINED_SESSIONS.values()
        )
        assert engine.pool.checkedout() == checked_out_before + 1
    finally:
        cleanup_release.set()
        never_close.set()
        release_task.cancel()
        await asyncio.gather(release_task, return_exceptions=True)
        pending = [
            task
            for db, task in atomic_execution._QUARANTINED_SESSIONS.values()
            if any(db is session for session, _close in sessions)
        ]
        await asyncio.gather(*pending, return_exceptions=True)
        for _db, close in sessions:
            await close()
    assert engine.pool.checkedout() == checked_out_before


@pytest.mark.parametrize("platform", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "generator_close"])
async def test_quarantined_request_dependency_does_not_reuse_or_close_connection(
    db_session, monkeypatch, platform, outcome
):
    assert db_session.bind is not None
    request = SimpleNamespace(state=SimpleNamespace())
    db = AsyncSessionLocal()
    calls = []
    release = asyncio.Event()
    original_invalidate = db.invalidate
    original_close = db.close

    for name in ("commit", "rollback", "close"):
        original = getattr(db, name)

        async def tracked(*, method=name, operation=original):
            calls.append(method)
            await operation()

        setattr(db, name, tracked)

    async def blocked_invalidate():
        await release.wait()
        await original_invalidate()

    db.invalidate = blocked_invalidate
    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: db)
    dependency = session_module.get_db(request)
    platform_dependency = None
    checked_out_before = engine.pool.checkedout()
    try:
        assert await anext(dependency) is db
        if platform:
            platform_dependency = session_module.get_platform_db(request, db)
            assert await anext(platform_dependency) is db
        await db.execute(select(1))
        await atomic_execution._discard_uncertain_session(db)
        assert engine.pool.checkedout() == checked_out_before + 1
        active_dependency = platform_dependency or dependency
        if outcome == "failure":
            with pytest.raises(RuntimeError, match="request failed after uncertainty"):
                await active_dependency.athrow(
                    RuntimeError("request failed after uncertainty")
                )
        elif outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await active_dependency.athrow(asyncio.CancelledError())
        elif outcome == "generator_close":
            await active_dependency.aclose()
        else:
            with pytest.raises(StopAsyncIteration):
                await anext(active_dependency)
        if platform:
            with pytest.raises(StopAsyncIteration):
                await anext(dependency)
        assert calls == [], "Request teardown must not touch a quarantined session"
        assert engine.pool.checkedout() == checked_out_before + 1
    finally:
        release.set()
        pending = [
            task
            for session, task in atomic_execution._QUARANTINED_SESSIONS.values()
            if session is db
        ]
        await asyncio.gather(*pending, return_exceptions=True)
        if platform_dependency is not None:
            await platform_dependency.aclose()
        await dependency.aclose()
        await original_close()
    assert engine.pool.checkedout() == checked_out_before
