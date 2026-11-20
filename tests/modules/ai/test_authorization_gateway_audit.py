"""Production Gateway audit admission uses one planned, original-identity write."""

import time
from dataclasses import replace

import pytest
from sqlalchemy import event, select

from app.core.exceptions import BusinessException
from app.db import authorization_chat_owner
from app.db.session import engine
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.auth.service import access_identity
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import authenticated_actor
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_completion_log_context import (
    context_case as context_case,
)
from tests.modules.ai.test_completion_log_context import (
    count_logs,
    start,
)


@pytest.fixture
async def audit_case(context_case):
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(context_case, db)
        context_case.deps.user = actor
        context_case.deps.tenant = access_identity(actor).tenant
        db.expunge_all()
    return context_case


async def test_actual_start_log_uses_complete_plan(audit_case):
    writes = []

    def capture(_conn, _cursor, sql, _params, context, _many):
        statement = context.compiled.statement if context.compiled else None
        table = getattr(statement, "table", None)
        if (
            sql.startswith("INSERT")
            and getattr(table, "name", None) == "ai_operation_log"
        ):
            writes.append(context.execution_options.get("_authorization_plan_permit"))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await start(audit_case)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert len(writes) == 1 and writes[0] is not None


async def test_start_log_rejects_original_user_version(audit_case):
    actor = audit_case.deps.user
    actor.__dict__["_verified_access_identity"] = replace(
        access_identity(actor), user_version=actor.auth_version + 1
    )
    with pytest.raises(executor.LogWriteError):
        await start(audit_case)
    assert await count_logs(audit_case) == 0


@pytest.mark.parametrize("after", [False, True])
async def test_start_log_never_retries_unknown_commit(audit_case, monkeypatch, after):
    original = authorization_chat_owner.commit_authorization_session
    commits = []

    async def unknown(db):
        commits.append(True)
        if after:
            await original(db)
        raise BusinessException(
            503, "unknown", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )

    monkeypatch.setattr(
        authorization_chat_owner, "commit_authorization_session", unknown
    )
    with pytest.raises(executor.LogWriteError):
        await start(audit_case)
    assert commits == [True]
    assert await count_logs(audit_case) == int(after)


async def test_actual_finish_log_uses_complete_plan(audit_case):
    log_id = await start(audit_case)
    writes = []

    def capture(_conn, _cursor, sql, _params, context, _many):
        statement = context.compiled.statement if context.compiled else None
        table = getattr(statement, "table", None)
        if (
            sql.startswith("UPDATE")
            and getattr(table, "name", None) == "ai_operation_log"
        ):
            writes.append(context.execution_options.get("_authorization_plan_permit"))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await executor._finish_log_final(
            log_id,
            ToolResult.failure("AI_REPEATED_FAILURE", "not executed"),
            time.monotonic(),
            tenant=audit_case.tenant,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert len(writes) == 1 and writes[0] is not None


@pytest.mark.parametrize("status", ["running", "succeeded"])
async def test_actual_finish_preserves_durable_owner(completion_case, status):
    case = completion_case
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        action.status = status
        log = await db.get(AiOperationLog, case.log_id)
        log.status = "success" if status == "succeeded" else "running"
        await db.flush()
        before = dict(
            (
                await db.execute(
                    select(AiOperationLog.__table__).where(
                        AiOperationLog.log_id == case.log_id,
                    )
                )
            )
            .mappings()
            .one()
        )
    await executor._finish_log_final(
        case.log_id,
        ToolResult.failure("TEST_FAILURE", "failure"),
        time.monotonic(),
        tenant=case.tenant,
    )
    async with AsyncSessionLocal() as db:
        after = dict(
            (
                await db.execute(
                    select(AiOperationLog.__table__).where(
                        AiOperationLog.log_id == case.log_id,
                    )
                )
            )
            .mappings()
            .one()
        )
    assert after == before


@pytest.mark.parametrize("after", [False, True])
async def test_finish_lost_ack_is_not_replayed(audit_case, monkeypatch, after):
    log_id = await start(audit_case)
    original = authorization_chat_owner.commit_authorization_session
    attempts = []

    async def unknown(db):
        attempts.append(True)
        if after:
            await original(db)
        raise BusinessException(
            503, "unknown", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )

    monkeypatch.setattr(
        authorization_chat_owner, "commit_authorization_session", unknown
    )
    await executor._finish_log_final(
        log_id,
        ToolResult.failure("TEST_FAILURE", "failure"),
        time.monotonic(),
        tenant=audit_case.tenant,
    )
    assert attempts == [True]
    async with AsyncSessionLocal() as db:
        log = await db.get(AiOperationLog, log_id)
        assert log.status == ("failed" if after else "pending_confirmation")
