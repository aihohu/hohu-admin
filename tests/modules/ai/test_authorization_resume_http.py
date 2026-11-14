"""Resume failure transitions are fixed-owner writes, never tool executions."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from app.core.exceptions import BusinessException
from app.db import authorization_lifecycle_owner
from app.db.authorization_http import get_authorization_runtime
from app.db.request_session import RequestSession
from app.db.session import engine
from app.modules.ai.api import resume
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.authorization_lifecycle_candidate import lifecycle_candidate
from app.modules.auth.service import access_identity
from app.modules.system.models.user import User
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import (
    approve,
    authenticated_actor,
    count_real_handler,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_completion_resume_guards import leave_claim_running
from tests.modules.ai.test_resume import _make_request


def _assert_optional_sse_result(chunks, *, error_code=None):
    events = [
        json.loads(line[6:])
        for chunk in chunks
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]
    results = [item for item in events if item["type"] == "tool_call_result"]
    assert events[-1]["type"] == "done"
    if results:
        assert len(results) == 1
        assert results[0]["ok"] is (error_code is None)
        assert results[0].get("errorCode") == error_code
    else:
        assert any(
            item.get("errorCode") == "AI_EXECUTION_OUTCOME_UNKNOWN" for item in events
        )
        assert events[-1]["persistence"] == "not_applicable"
        assert events[-1]["projection"] == "unchanged"


@pytest.mark.parametrize("stall_projection", [False, True])
async def test_actual_resume_replays_completed_action_with_strict_request_session(
    completion_case, monkeypatch, stall_projection
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    assert (await approve(case)).data.status == "succeeded"
    monkeypatch.setattr(resume.settings, "AI_SSE_RESUME_ENABLED", True)
    locks = []
    projection_entered = asyncio.Event()

    async def stalled_read(*_args, **_kwargs):
        projection_entered.set()
        await asyncio.Event().wait()

    if stall_projection:
        monkeypatch.setattr(lifecycle_candidate, "resume_read", stalled_read)

    def capture(_conn, _cursor, sql, _params, _context, _many):
        if "FOR SHARE" in sql or "FOR UPDATE" in sql:
            locks.append(sql)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with RequestSession(bind=engine, expire_on_commit=False) as db:
            actor = await authenticated_actor(case, db)
            response = await resume.resume_chat(
                request=_make_request(case.confirmation_id),
                confirmation_id_query=case.confirmation_id,
                db=db,
                current_user=actor,
            )
            chunks = [chunk async for chunk in response.body_iterator]
            _assert_optional_sse_result(chunks)
            if stall_projection:
                assert projection_entered.is_set()
                assert any("AI_EXECUTION_OUTCOME_UNKNOWN" in chunk for chunk in chunks)
            assert db._request_released and not db.in_transaction()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    assert len(case.calls) == 1
    async with AsyncSessionLocal() as db:
        assert (await db.get(AiPreparedAction, case.action_id)).status == "succeeded"
        assert (await db.get(AiOperationLog, case.log_id)).status == "success"


async def test_pending_resume_releases_request_before_transport_wait(
    completion_case, monkeypatch
):
    case = completion_case
    monkeypatch.setattr(resume.settings, "AI_SSE_RESUME_ENABLED", True)
    monkeypatch.setattr(resume.settings, "AI_HITL_MODE", "redis_pubsub")
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        pending = resume._pending_from_durable_action(
            await db.get(AiPreparedAction, case.action_id)
        )

        async def hang(*_args, **_kwargs):
            assert not db.in_transaction()
            assert db._request_released
            raise TimeoutError()

        waiter = AsyncMock(side_effect=hang)
        monkeypatch.setattr(resume.hitl_manager, "hang", waiter)
        monkeypatch.setattr(
            resume.hitl_manager, "get_pending", AsyncMock(return_value=pending)
        )
        monkeypatch.setattr(resume.hitl_manager, "ttl", AsyncMock(return_value=240))
        monkeypatch.setattr(resume.redis_client, "set", AsyncMock(return_value=True))
        monkeypatch.setattr(resume.redis_client, "eval", AsyncMock(return_value=1))
        response = await resume.resume_chat(
            request=_make_request(case.confirmation_id),
            confirmation_id_query=case.confirmation_id,
            db=db,
            current_user=actor,
        )
        chunks = [chunk async for chunk in response.body_iterator]
        waiter.assert_awaited_once()
        assert any("AI_HITL_TIMEOUT" in chunk for chunk in chunks)


async def test_resume_terminal_read_uses_global_guard_without_commit(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    assert (await approve(case)).data.status == "succeeded"
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        pending = resume._pending_from_durable_action(
            await db.get(AiPreparedAction, case.action_id)
        )
    locks, commits = [], []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        if "FOR SHARE" in sql or "FOR UPDATE" in sql:
            locks.append(sql)

    def commit(_conn):
        commits.append(True)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    event.listen(engine.sync_engine, "commit", commit)
    try:
        # Exercise the real owner under its eight-second authorization budget.
        # The separate optional-display deadline can legitimately return unknown.
        events = await get_authorization_runtime().lifecycle.resume_read(
            case.confirmation_id,
            tenant=case.tenant,
            actor_user_version=access_identity(actor).user_version,
            resume_binding=resume._resume_binding(pending),
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        event.remove(engine.sync_engine, "commit", commit)
    assert events[0].type == "tool_call_result"
    assert events[0].ok is True
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    assert commits == []
    assert len(case.calls) == 1


@pytest.mark.parametrize("stall_projection", [False, True])
async def test_resume_pending_failure_enters_global_guard_first(
    completion_case, monkeypatch, stall_projection
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        pending = resume._pending_from_durable_action(
            await db.get(AiPreparedAction, case.action_id)
        )
    locks = []
    projection_entered = asyncio.Event()
    projection_cancelled = asyncio.Event()
    real_read = lifecycle_candidate.resume_read

    async def stalled_read(*_args, **_kwargs):
        projection_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            projection_cancelled.set()

    if stall_projection:
        monkeypatch.setattr(lifecycle_candidate, "resume_read", stalled_read)

    def capture(_conn, _cursor, sql, _params, _context, _many):
        if "FOR UPDATE" in sql or "FOR SHARE" in sql:
            locks.append(sql)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        events = await resume._terminalize_durable_resume_failure(
            confirmation_id=case.confirmation_id,
            pending=pending,
            user_id=case.tenant.actor_user_id,
            current_user=actor,
            tenant=case.tenant,
            error_code="AI_HITL_TIMEOUT",
            error_msg="The confirmation wait timed out",
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    async with AsyncSessionLocal() as db:
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        assert action.status == "expired"
        assert log.status == "expired"
        assert action.error_code == log.error_code == "AI_HITL_TIMEOUT"
        assert action.execution_owner is None
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    if stall_projection:
        assert projection_entered.is_set() and projection_cancelled.is_set()
        monkeypatch.setattr(lifecycle_candidate, "resume_read", real_read)
        assert events[0].type == "ai_error"
        assert events[0].error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
    if events[0].type == "ai_error":
        assert len(events) == 2
        assert events[0].error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
        assert events[-1].persistence == "not_applicable"
        assert events[-1].projection == "unchanged"
    else:
        assert events[0].type == "tool_call_result"
        assert events[0].ok is False
        assert events[0].error_code == "AI_HITL_TIMEOUT"
    # Always prove the actual terminal projection succeeds through the real
    # authorization owner, including after the optional one-second read failed.
    terminal = await get_authorization_runtime().lifecycle.resume_read(
        case.confirmation_id,
        tenant=case.tenant,
        actor_user_version=access_identity(actor).user_version,
        resume_binding=resume._resume_binding(pending),
    )
    assert terminal[0].type == "tool_call_result"
    assert terminal[0].ok is False
    assert terminal[0].error_code == "AI_HITL_TIMEOUT"
    assert case.calls == []


@pytest.mark.parametrize(
    "fault",
    ["source", "trace", "tool", "args", "owner", "tenant", "version", "running"],
)
async def test_resume_failure_cannot_change_invalid_or_claimed_action(
    completion_case, monkeypatch, fault
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    if fault == "running":
        await leave_claim_running(case, monkeypatch)
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        version = access_identity(actor).user_version
        action = await db.get(AiPreparedAction, case.action_id)
        pending = resume._pending_from_durable_action(action)
        before = (action.status, action.row_version, action.execution_owner)
    replacements = {
        "source": {"source_user_message_id": pending.source_user_message_id + 1},
        "trace": {"trace_id": "wrong-trace"},
        "tool": {"tool_name": "user.update"},
        "args": {"args": {}},
        "owner": {"user_id": pending.user_id + 1},
        "tenant": {"tenant_id": pending.tenant_id + 1},
    }
    pending = replace(pending, **replacements.get(fault, {}))
    try:
        if fault == "version":
            async with AsyncSessionLocal() as db, db.begin():
                (await db.get(User, case.tenant.actor_user_id)).auth_version = (
                    version + 1
                )
        events = await resume._terminalize_durable_resume_failure(
            confirmation_id=case.confirmation_id,
            pending=pending,
            user_id=case.tenant.actor_user_id,
            current_user=actor,
            tenant=case.tenant,
            error_code="AI_HITL_TIMEOUT",
            error_msg="Confirmation timeout",
        )
        assert events[0].error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
        assert events[-1].persistence == "not_applicable"
        async with AsyncSessionLocal() as db:
            action = await db.get(AiPreparedAction, case.action_id)
            assert (action.status, action.row_version, action.execution_owner) == before
        assert case.calls == []
    finally:
        if fault == "version":
            async with AsyncSessionLocal() as db, db.begin():
                (await db.get(User, case.tenant.actor_user_id)).auth_version = version


@pytest.mark.parametrize("commit_reached", [False, True])
async def test_resume_failure_unknown_ack_does_not_publish_or_reexecute(
    completion_case, monkeypatch, commit_reached
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        pending = resume._pending_from_durable_action(
            await db.get(AiPreparedAction, case.action_id)
        )
    commit = authorization_lifecycle_owner.commit_authorization_session

    async def unknown(db):
        if commit_reached:
            await commit(db)
        raise BusinessException(
            503, "Unknown ACK", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )

    monkeypatch.setattr(
        authorization_lifecycle_owner, "commit_authorization_session", unknown
    )
    events = await resume._terminalize_durable_resume_failure(
        confirmation_id=case.confirmation_id,
        pending=pending,
        user_id=case.tenant.actor_user_id,
        current_user=actor,
        tenant=case.tenant,
        error_code="AI_HITL_TIMEOUT",
        error_msg="Confirmation timeout",
    )
    assert events[0].error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
    assert events[-1].persistence == "not_applicable"
    async with AsyncSessionLocal() as db:
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        assert (
            action.status
            == log.status
            == ("expired" if commit_reached else "pending_confirmation")
        )
        if commit_reached:
            assert action.error_code == log.error_code == "AI_HITL_TIMEOUT"
    assert case.calls == []
