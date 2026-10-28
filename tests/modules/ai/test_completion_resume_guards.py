"""Durable completion wins over transport state and stale terminal snapshots."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.db import authorization_chat_owner
from app.db.authorization_owner import _QUARANTINED
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai import lifecycle
from app.modules.ai.agents.gateway.authorization_execution import PlannedGatewayOwner
from app.modules.ai.agents.hitl.events import (
    AiErrorEvent,
    DoneEvent,
    ToolCallResultEvent,
)
from app.modules.ai.api import confirm, resume
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.authorization_execution_candidate import (
    ExecutionDisposition,
)
from app.modules.ai.service.authorization_lifecycle_candidate import lifecycle_candidate
from app.modules.auth.service import access_identity
from tests.modules.ai.test_atomic_completion_guards import PLATFORM
from tests.modules.ai.test_atomic_prepared_completion import (
    approve,
    authenticated_actor,
    count_real_handler,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_resume import _make_request


@pytest.mark.parametrize("status", ["rejected", "expired"])
@pytest.mark.parametrize("corrupt", [False, True])
@pytest.mark.parametrize("denial", ["chat", "projection"])
async def test_minimal_cancelled_resume_requires_never_started_proof(
    completion_case, monkeypatch, status, corrupt, denial
):
    case = completion_case
    if status == "rejected":
        async with AsyncSessionLocal() as db:
            actor = await authenticated_actor(case, db)
            await confirm.confirm_tool(
                confirm.ConfirmRequest(
                    confirmation_id=case.confirmation_id, action="reject"
                ),
                db,
                actor,
            )
    else:
        async with AsyncSessionLocal() as db, db.begin():
            (await db.get(AiPreparedAction, case.action_id)).expires_at = datetime.now(
                UTC
            ) - timedelta(seconds=1)
        await lifecycle.cleanup_prepared_actions_on_startup(
            redis_module.redis_client, platform=PLATFORM, expired_only=True
        )
    if corrupt:
        async with AsyncSessionLocal() as db, db.begin():
            (await db.get(AiPreparedAction, case.action_id)).row_version = 4
    monkeypatch.setattr(resume.settings, "AI_SSE_RESUME_ENABLED", True)
    monkeypatch.setattr(
        resume.hitl_manager, "get_pending", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        resume, "has_explicit_permission", lambda *_args: denial != "chat"
    )
    monkeypatch.setattr(
        resume.result_projection_service,
        "authorize_result_projection",
        AsyncMock(return_value=False),
    )
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        response = await resume.resume_chat(
            request=_make_request(case.confirmation_id),
            confirmation_id_query=case.confirmation_id,
            db=db,
            current_user=actor,
        )
    assert response.data.status == ("running" if corrupt else status)
    assert (response.data.finished_at is None) is corrupt
    assert case.calls == []


async def test_unknown_notice_never_waits_for_stalled_close(
    completion_case, monkeypatch
):
    case = completion_case
    await leave_claim_running(case, monkeypatch)
    async with AsyncSessionLocal() as db:
        identity = access_identity(await authenticated_actor(case, db))
        pending = resume._pending_from_durable_action(
            await db.get(AiPreparedAction, case.action_id)
        )
    release = asyncio.Event()
    committed, stalled = [], []
    original_commit = authorization_chat_owner.commit_authorization_session
    original_close = AsyncSession.close

    async def commit(db):
        await original_commit(db)
        committed.append(db)

    async def resistant_close(db):
        await original_close(db)
        if db in committed and not stalled:
            stalled.append(db)
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

    monkeypatch.setattr(
        authorization_chat_owner, "commit_authorization_session", commit
    )
    monkeypatch.setattr(AsyncSession, "close", resistant_close)
    try:
        task = asyncio.create_task(
            resume._persist_unknown_resume_notice(
                pending, tenant=case.tenant, actor_user_version=identity.user_version
            )
        )
        done, _ = await asyncio.wait({task}, timeout=1.5)
        assert task in done, "Session disposal must not exceed the display budget"
        assert task.result()[-1].persistence == "committed"
        assert len(stalled) == 1
        assert any(row[0] is stalled[0] for row in _QUARANTINED.values())
    finally:
        release.set()
        await task
        tasks = [row[2] for row in _QUARANTINED.values() if row[0] in stalled]
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


async def test_known_terminal_projection_timeout_preserves_facts(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    await approve(case)
    async with AsyncSessionLocal() as db:
        pending = resume._pending_from_durable_action(
            await db.get(AiPreparedAction, case.action_id)
        )
        actor = await authenticated_actor(case, db)
    release = asyncio.Event()

    async def stalled_proof(*_args, **_kwargs):
        await release.wait()

    monkeypatch.setattr(lifecycle_candidate, "resume_read", stalled_proof)
    task = asyncio.create_task(
        resume._load_durable_resume_terminal(
            confirmation_id=case.confirmation_id,
            pending=pending,
            user_id=case.tenant.actor_user_id,
            tenant=case.tenant,
            current_user=actor,
        )
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=2)
        assert task in done, "Optional display must fit its one-second budget"
        events = task.result()
        assert events[-1].persistence == "not_applicable"
        assert not any(isinstance(item, ToolCallResultEvent) for item in events)
    finally:
        release.set()
        await task
    async with AsyncSessionLocal() as db:
        assert (await db.get(AiPreparedAction, case.action_id)).status == "succeeded"
        assert (await db.get(AiOperationLog, case.log_id)).status == "success"
    assert len(case.calls) == 1


async def leave_claim_running(case, monkeypatch):
    monkeypatch.setattr(
        PlannedGatewayOwner,
        "execute_claim",
        AsyncMock(return_value=ExecutionDisposition("unknown")),
    )
    assert (await approve(case)).data.status == "running"


@pytest.mark.parametrize("cache", ["missing", "unavailable", "old_wake", "short_ttl"])
async def test_running_resume_ignores_hot_cache_admission(
    completion_case, monkeypatch, cache
):
    case = completion_case
    await leave_claim_running(case, monkeypatch)
    monkeypatch.setattr(resume.settings, "AI_SSE_RESUME_ENABLED", True)
    async with AsyncSessionLocal() as db:
        action = await db.get(AiPreparedAction, case.action_id)
        pending = resume._pending_from_durable_action(action)
        if cache == "old_wake":
            pending = replace(pending, wake_action="approved")
        get_pending = AsyncMock(
            return_value=None if cache == "missing" else pending,
            side_effect=RedisConnectionError("hot cache unavailable")
            if cache == "unavailable"
            else None,
        )
        monkeypatch.setattr(resume.hitl_manager, "get_pending", get_pending)
        monkeypatch.setattr(resume.hitl_manager, "ttl", AsyncMock(return_value=1))
        actor = await authenticated_actor(case, db)
        response = await resume.resume_chat(
            request=_make_request(case.confirmation_id),
            confirmation_id_query=case.confirmation_id,
            db=db,
            current_user=actor,
        )
        chunks = [chunk async for chunk in response.body_iterator]
    assert any("AI_EXECUTION_OUTCOME_UNKNOWN" in chunk for chunk in chunks)
    assert any(
        '"type": "done"' in chunk or '"type":"done"' in chunk for chunk in chunks
    )
    assert not any("tool_call_result" in chunk for chunk in chunks)
    get_pending.assert_not_awaited()
    assert case.calls == []


@pytest.mark.parametrize(
    "corruption",
    [
        "log_hash",
        "missing_log",
        "failed_owner",
        "legacy_failed",
        "file_failed",
        "rejected_started",
        "expired_started",
    ],
)
async def test_resume_does_not_project_unproven_terminal(
    completion_case, monkeypatch, corruption
):
    case = completion_case
    count_real_handler(
        case,
        monkeypatch,
        return_failure=corruption in {"failed_owner", "legacy_failed", "file_failed"},
    )
    await approve(case)
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        pending = resume._pending_from_durable_action(action)
        if corruption == "missing_log":
            await db.delete(log)
        elif corruption == "failed_owner":
            action.execution_owner = "v0162-db:invalid"
        elif corruption == "legacy_failed":
            action.execution_owner = None
        elif corruption == "file_failed":
            action.execution_owner = None
            action.execute_tool_name = log.tool_name = "user.export"
            pending = resume._pending_from_durable_action(action)
        elif corruption in {"rejected_started", "expired_started"}:
            action.status = log.status = corruption.split("_")[0]
        else:
            log.args_hash = "f" * 64
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        events = await resume._load_durable_resume_terminal(
            confirmation_id=case.confirmation_id,
            pending=pending,
            user_id=case.tenant.actor_user_id,
            tenant=case.tenant,
            current_user=actor,
        )
    assert any(
        isinstance(event, AiErrorEvent)
        and event.error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
        for event in events
    )
    assert not any(isinstance(event, ToolCallResultEvent) for event in events)
    assert len(case.calls) == 1


@pytest.mark.parametrize("write_fails", [False, True])
async def test_unknown_notice_only_acknowledges_confirmed_message_commit(
    completion_case, monkeypatch, write_fails
):
    case = completion_case
    await leave_claim_running(case, monkeypatch)
    async with AsyncSessionLocal() as db:
        identity = access_identity(await authenticated_actor(case, db))
        pending = resume._pending_from_durable_action(
            await db.get(AiPreparedAction, case.action_id)
        )

    locks = []

    def fail_notice(_conn, _cursor, statement, _params, _context, _many):
        if "FOR SHARE" in statement or "FOR UPDATE" in statement:
            locks.append(statement)
        if write_fails and statement.startswith("INSERT INTO ai_message"):
            raise RuntimeError("Injected unknown-notice SQL failure")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_notice)
    try:
        events = await resume._persist_unknown_resume_notice(
            pending, tenant=case.tenant, actor_user_version=identity.user_version
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_notice)
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    assert done.persistence == ("not_applicable" if write_fails else "committed")
    assert not any(isinstance(event, ToolCallResultEvent) for event in events)
    async with AsyncSessionLocal() as db:
        message = await db.scalar(
            select(AiMessage).where(
                AiMessage.conversation_id == case.conversation_id,
                AiMessage.role == "assistant",
            )
        )
        assert (message is None) is write_fails
        assert (await db.get(AiPreparedAction, case.action_id)).status == "running"
        if message is not None:
            assert message.resolver_version is not None
            assert message.tool_codes == []
