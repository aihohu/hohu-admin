"""Read surfaces share strict completion facts without changing durable rows."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationException, NotFoundException
from app.db import authorization_completion_facts_owner as read_module
from app.db import authorization_owner
from app.db.authorization_completion_facts_owner import CompletionFactsOwner
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.session import engine
from app.modules.ai.agents.gateway.atomic_execution import OUTCOME_UNKNOWN_MESSAGE
from app.modules.ai.agents.hitl.constants import PreparedActionStatus
from app.modules.ai.api.confirm import confirm_tool
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.confirm import ConfirmRequest
from app.modules.ai.schemas.operation_log import TraceListQuery
from app.modules.ai.service.atomic_completion_service import new_execution_owner
from app.modules.ai.service.completion_read_state_service import (
    completion_read_state_service,
    effective_call_state,
    effective_log_state,
)
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.operation_status_projection import (
    project_operation as get_operation_log,
)
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.ai.service.trace_service import trace_service
from app.modules.auth.service import load_live_user_authority
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import (
    approve,
    authenticated_actor,
    count_real_handler,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)


async def stored_facts(case):
    async with AsyncSessionLocal() as db:
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        message = await db.scalar(
            select(AiMessage).where(
                AiMessage.conversation_id == case.conversation_id,
                AiMessage.role == "assistant",
            )
        )
        return (
            action.status if action else None,
            action.row_version if action else None,
            action.execution_owner if action else None,
            action.finished_at if action else None,
            log.status,
            log.error_code,
            log.finished_at,
            message.content if message else None,
            deepcopy(message.tool_calls) if message else None,
        )


@pytest.mark.parametrize("log_code", [None, "USER_REJECTED", "AI_INTERNAL_ERROR"])
async def test_verified_rejection_keeps_cancel_reason_on_all_read_surfaces(
    completion_case, monkeypatch, log_code
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        response = await confirm_tool(
            ConfirmRequest(confirmation_id=case.confirmation_id, action="reject"),
            db=db,
            current_user=actor,
        )
        assert response.data.status == "rejected"
    async with AsyncSessionLocal() as db, db.begin():
        log = await db.get(AiOperationLog, case.log_id)
        log.error_code = log_code
        trace_id = log.trace_id
    before = await stored_facts(case)
    expected_code = (
        "AI_EXECUTION_OUTCOME_UNKNOWN"
        if log_code == "AI_INTERNAL_ERROR"
        else "USER_REJECTED"
    )
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        poll = await get_operation_log(
            tool_call_id=case.tool_call_id,
            db=db,
            current_user=actor,
            tenant=case.tenant,
        )
        detail = await trace_service.get_trace(
            db, tenant=case.tenant, trace_id=trace_id
        )
        history = await conversation_service.project_messages(
            db, conversation_id=case.conversation_id, current_user=actor
        )
        await db.commit()
    assert poll.data.error_code == detail.operations[0].error_code == expected_code
    assistant = next(message for message in history if message.role == "assistant")
    call = next(
        row for row in assistant.tool_calls if row["tool_call_id"] == case.tool_call_id
    )
    if log_code == "AI_INTERNAL_ERROR":
        assert poll.data.status == "running"
        assert "ok" not in call and "error_code" not in call
        assert OUTCOME_UNKNOWN_MESSAGE in assistant.content
    else:
        assert poll.data.status == "rejected"
        assert call["ok"] is False and call["error_code"] == "USER_REJECTED"
    assert case.calls == []
    assert await stored_facts(case) == before


@pytest.mark.parametrize(
    "corruption", ["success_log", "failed_owner", "file_failed", "missing_action"]
)
async def test_poll_trace_history_mask_unproven_terminal_without_writing(
    completion_case, monkeypatch, corruption
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=corruption != "success_log")
    await approve(case)
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        if corruption == "success_log":
            log.args_hash = "f" * 64
        elif corruption == "failed_owner":
            action.execution_owner = "v0162-db:invalid"
        elif corruption == "file_failed":
            action.execute_tool_name = log.tool_name = "user.export"
            action.execution_owner = new_execution_owner("user.export")
        message = await db.scalar(
            select(AiMessage).where(
                AiMessage.conversation_id == case.conversation_id,
                AiMessage.role == "assistant",
            )
        )
        assert message is not None
        message.content = "已有可见说明。"
        trace_id = action.trace_id
        if corruption == "missing_action":
            await db.delete(action)
    before = await stored_facts(case)
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        poll = await get_operation_log(
            tool_call_id=case.tool_call_id,
            db=db,
            current_user=actor,
            tenant=case.tenant,
        )
        detail = await trace_service.get_trace(
            db, tenant=case.tenant, trace_id=trace_id
        )
        listing = await trace_service.list_traces(
            db,
            tenant=case.tenant,
            query=TraceListQuery(trace_id=trace_id, status="running"),
        )
        hidden_terminal = await trace_service.list_traces(
            db,
            tenant=case.tenant,
            query=TraceListQuery(trace_id=trace_id, status=before[4]),
        )
        history = await conversation_service.project_messages(
            db, conversation_id=case.conversation_id, current_user=actor
        )
        await db.commit()
    assert poll.data.status == "running"
    assert poll.data.error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
    assert poll.data.finished_at is None and poll.data.duration_ms is None
    operation = detail.operations[0]
    assert operation.status == "running"
    assert operation.error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
    assert operation.finished_at is None and operation.duration_ms is None
    assert listing.total == 1
    assert listing.records[0].statuses == ["running"]
    assert listing.records[0].finished_at is None
    assert hidden_terminal.total == 0
    assistant = next(message for message in history if message.role == "assistant")
    assert "已有可见说明。" in assistant.content
    assert assistant.content.count(OUTCOME_UNKNOWN_MESSAGE) == 1
    assert assistant.completed_tool_call_ids == []
    for call in assistant.tool_calls or []:
        if call.get("tool_call_id") == case.tool_call_id:
            assert not set(call) & {"ok", "error_code", "duration_ms", "result", "ui"}
    assert await stored_facts(case) == before


@pytest.mark.parametrize(
    "corruption", ["success_log", "failed_owner", "file_failed", "missing_action"]
)
async def test_unknown_completion_blocks_conversation_admission(
    completion_case, monkeypatch, corruption
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=corruption != "success_log")
    await approve(case)
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        if corruption == "success_log":
            log.args_hash = "f" * 64
        elif corruption == "failed_owner":
            action.execution_owner = "v0162-db:invalid"
        elif corruption == "file_failed":
            action.execute_tool_name = log.tool_name = "user.export"
            action.execution_owner = new_execution_owner("user.export")
        else:
            await db.delete(action)
    before = await stored_facts(case)
    async with AsyncSessionLocal() as db:
        assert await prepared_action_service.has_in_progress_for_conversation(
            db,
            conversation_id=case.conversation_id,
            user_id=case.tenant.actor_user_id,
            tenant=case.tenant,
        )
        assert not await prepared_action_service.has_in_progress_for_conversation(
            db,
            conversation_id=case.conversation_id,
            user_id=case.tenant.actor_user_id,
            tenant=replace(case.tenant, tenant_id=99999),
        )
        await db.commit()
    assert await stored_facts(case) == before


@pytest.mark.parametrize("failure", [False, True])
async def test_valid_db_completion_retains_terminal_poll_and_trace(
    completion_case, monkeypatch, failure
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=failure)
    await approve(case)
    before = await stored_facts(case)
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        action = await db.get(AiPreparedAction, case.action_id)
        poll = await get_operation_log(
            tool_call_id=case.tool_call_id,
            db=db,
            current_user=actor,
            tenant=case.tenant,
        )
        detail = await trace_service.get_trace(
            db, tenant=case.tenant, trace_id=action.trace_id
        )
    expected = "failed" if failure else "success"
    assert poll.data.status == detail.operations[0].status == expected
    assert poll.data.finished_at is not None
    async with AsyncSessionLocal() as db:
        assert not await prepared_action_service.has_in_progress_for_conversation(
            db,
            conversation_id=case.conversation_id,
            user_id=case.tenant.actor_user_id,
            tenant=case.tenant,
        )
    assert await stored_facts(case) == before


async def test_committed_proof_refresh_ignores_stale_request_identity_map(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    await approve(case)
    async with AsyncSessionLocal() as request:
        actor = await load_live_user_authority(request, tenant=case.tenant)
        stale = await request.get(AiOperationLog, case.log_id)
        original_hash = stale.args_hash
        async with AsyncSessionLocal() as writer, writer.begin():
            current = await writer.get(AiOperationLog, case.log_id)
            current.args_hash = "f" * 64
        result = await get_operation_log(
            tool_call_id=case.tool_call_id,
            db=request,
            current_user=actor,
            tenant=case.tenant,
        )
        assert stale.args_hash == original_hash
        assert result.data.error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
        assert result.data.status == "running"


async def test_missing_log_remains_unknown_without_creating_a_receipt(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    await approve(case)
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            delete(AiOperationLog).where(AiOperationLog.log_id == case.log_id)
        )
    states = await completion_read_state_service.read(
        tenant=case.tenant, tool_call_ids=[case.tool_call_id]
    )
    assert states[case.tool_call_id].unknown
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        messages = await conversation_service.project_messages(
            db, conversation_id=case.conversation_id, current_user=actor
        )
        assistant = next(item for item in messages if item.role == "assistant")
        assert OUTCOME_UNKNOWN_MESSAGE in assistant.content
        assert assistant.completed_tool_call_ids == []
        assert await prepared_action_service.has_in_progress_for_conversation(
            db,
            conversation_id=case.conversation_id,
            user_id=case.tenant.actor_user_id,
            tenant=case.tenant,
        )


async def test_missing_audit_poll_keeps_owner_unknown_and_tenant_isolation(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    await approve(case)
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            delete(AiOperationLog).where(AiOperationLog.log_id == case.log_id)
        )
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        # Exercise the owner branch with no audit or chat permission; the
        # authenticated ID is real, and this transport principal grants nothing.
        owner = SimpleNamespace(
            user_id=actor.user_id, user_name=actor.user_name, roles=[]
        )
        response = await get_operation_log(
            tool_call_id=case.tool_call_id,
            db=db,
            current_user=owner,
            tenant=case.tenant,
        )
        assert response.data.model_dump(by_alias=True) == {
            "toolCallId": case.tool_call_id,
            "status": "running",
            "errorCode": "AI_EXECUTION_OUTCOME_UNKNOWN",
            "finishedAt": None,
        }
        outsider_tenant = replace(case.tenant, actor_user_id=case.created_user_ids[0])
        outsider = await load_live_user_authority(db, tenant=outsider_tenant)
        with pytest.raises(AuthorizationException) as denied:
            await get_operation_log(
                tool_call_id=case.tool_call_id,
                db=db,
                current_user=outsider,
                tenant=outsider_tenant,
            )
        assert denied.value.error_code == "AI_OPERATION_LOG_FORBIDDEN"
        with pytest.raises(NotFoundException):
            await get_operation_log(
                tool_call_id=case.tool_call_id,
                db=db,
                current_user=actor,
                tenant=replace(case.tenant, tenant_id=99999),
            )
        await db.commit()
    async with AsyncSessionLocal() as observer:
        assert await observer.get(AiOperationLog, case.log_id) is None
        action = await observer.get(AiPreparedAction, case.action_id)
        assert action.status == "succeeded"


@pytest.mark.parametrize("status", ["rejected", "expired"])
@pytest.mark.parametrize("tool", ["user.create", "user.export"])
async def test_cancelled_write_requires_never_started_proof_for_reads_and_admission(
    completion_case, status, tool
):
    case = completion_case
    async with AsyncSessionLocal() as db, db.begin():
        rejected = status == "rejected"
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        action.execute_tool_name = log.tool_name = tool
        action = await prepared_action_service.transition_status(
            db,
            action_id=case.action_id,
            expected_status=PreparedActionStatus.PENDING_CONFIRMATION,
            expected_version=1,
            target_status=PreparedActionStatus(status),
            approved_by=case.tenant.actor_user_id if rejected else None,
            error_code="USER_REJECTED" if rejected else "AI_HITL_EXPIRED",
            tenant=case.tenant,
        )
        assert action is not None
        if rejected:
            await operation_log_service.mark_rejected_if_pending(
                db,
                case.log_id,
                approved_by=case.tenant.actor_user_id,
                tenant=case.tenant,
            )
        else:
            await operation_log_service.mark_expired_if_pending(
                db, case.log_id, tenant=case.tenant
            )
        trace_id = action.trace_id
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        poll = await get_operation_log(
            tool_call_id=case.tool_call_id,
            db=db,
            current_user=actor,
            tenant=case.tenant,
        )
        assert poll.data.status == status
        assert not await prepared_action_service.has_in_progress_for_conversation(
            db,
            conversation_id=case.conversation_id,
            user_id=case.tenant.actor_user_id,
            tenant=case.tenant,
        )
    # A terminal label alone cannot prove pending(1) -> cancelled(2).
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        action.row_version = 4
    for missing_action in (False, True):
        if missing_action:
            async with AsyncSessionLocal() as db, db.begin():
                await db.delete(await db.get(AiPreparedAction, case.action_id))
        async with AsyncSessionLocal() as db:
            actor = await authenticated_actor(case, db)
            poll = await get_operation_log(
                tool_call_id=case.tool_call_id,
                db=db,
                current_user=actor,
                tenant=case.tenant,
            )
            assert poll.data.status == "running"
            assert poll.data.error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
            detail = await trace_service.get_trace(
                db, tenant=case.tenant, trace_id=trace_id
            )
            assert detail.operations[0].status == "running"
            assert detail.operations[0].error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
            assert await prepared_action_service.has_in_progress_for_conversation(
                db,
                conversation_id=case.conversation_id,
                user_id=case.tenant.actor_user_id,
                tenant=case.tenant,
            )
            await db.commit()
        async with AsyncSessionLocal() as observer:
            assert (await observer.get(AiOperationLog, case.log_id)).status == status


async def test_unknown_history_denied_projection_is_minimal_and_idempotent(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    await approve(case)
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        action.snapshot_hash = "f" * 64
        message = await db.scalar(
            select(AiMessage).where(
                AiMessage.conversation_id == case.conversation_id,
                AiMessage.role == "assistant",
            )
        )
        message.content = f"private result {OUTCOME_UNKNOWN_MESSAGE}"
    monkeypatch.setattr(
        result_projection_service,
        "authorize_message_projection",
        AsyncMock(return_value=False),
    )
    before = await stored_facts(case)
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        for _ in range(2):
            messages = await conversation_service.project_messages(
                db, conversation_id=case.conversation_id, current_user=actor
            )
            assistant = next(item for item in messages if item.role == "assistant")
            assert (
                assistant.content
                == f"{OUTCOME_UNKNOWN_MESSAGE}\n[AI_EXECUTION_OUTCOME_UNKNOWN] toolCallId={case.tool_call_id}"
            )
            assert assistant.parts is None
            assert assistant.completed_tool_call_ids == []
            assert "private result" not in assistant.model_dump_json()
        await db.commit()
    assert await stored_facts(case) == before


async def test_batch_proof_is_one_read_only_statement_without_row_locks(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    await approve(case)
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.upper())

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        states = await completion_read_state_service.read(
            tenant=case.tenant, tool_call_ids=[case.tool_call_id] * 100
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert states[case.tool_call_id].verified
    assert len([sql for sql in statements if sql.lstrip().startswith("SELECT")]) == 1
    assert any(
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY" in sql
        for sql in statements
    )
    assert all("FOR UPDATE" not in sql for sql in statements)
    assert all(
        not sql.lstrip().startswith(("UPDATE", "INSERT", "DELETE"))
        for sql in statements
    )


@pytest.mark.parametrize("failure", [False, True])
async def test_history_uses_verified_state_instead_of_stale_tool_card(
    completion_case, monkeypatch, failure
):
    case = completion_case
    count_real_handler(case, monkeypatch, return_failure=failure)
    await approve(case)
    async with AsyncSessionLocal() as db, db.begin():
        message = await db.scalar(
            select(AiMessage).where(
                AiMessage.conversation_id == case.conversation_id,
                AiMessage.role == "assistant",
            )
        )
        message.tool_calls = [
            {
                "tool": "user.create",
                "tool_call_id": case.tool_call_id,
                "ok": failure,
                "error_code": "STALE_CARD",
                "result": {"stale": True},
                "ui": {"stale": True},
            }
        ]
    before = await stored_facts(case)
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        messages = await conversation_service.project_messages(
            db, conversation_id=case.conversation_id, current_user=actor
        )
        assistant = next(item for item in messages if item.role == "assistant")
        call = assistant.tool_calls[0]
        assert call["ok"] is not failure
        assert call.get("error_code") == (
            "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED" if failure else None
        )
        assert "result" not in call and "ui" not in call
        await db.commit()
    assert await stored_facts(case) == before


async def test_snapshot_timeout_quarantines_session_and_preserves_readonly(monkeypatch):
    gate = asyncio.Event()

    async def hangs(*_args, **_kwargs):
        await gate.wait()

    db = AuthorizationRuntime(engine).sessions()

    async def resistant_rollback():
        while not gate.is_set():
            try:
                await gate.wait()
            except asyncio.CancelledError:
                continue

    monkeypatch.setattr(db, "execute", AsyncMock(side_effect=hangs))
    monkeypatch.setattr(db, "rollback", resistant_rollback)
    monkeypatch.setattr(AsyncSession, "invalidate", AsyncMock(side_effect=hangs))
    monkeypatch.setattr(
        "app.db.authorization_http.get_authorization_runtime",
        lambda: SimpleNamespace(completion_facts=CompletionFactsOwner(lambda: db)),
    )
    monkeypatch.setattr(read_module, "_READ_TIMEOUT_SEC", 0.02)
    monkeypatch.setattr(authorization_owner, "_CLEANUP_SECONDS", 0.02)
    try:
        async with asyncio.timeout(1):
            states = await completion_read_state_service.read(
                tenant=SimpleNamespace(tenant_id=0), tool_call_ids=["read", "write"]
            )
        readonly = SimpleNamespace(
            tool_call_id="read",
            tool_name="user.lookup",
            readonly_snapshot=True,
            status="failed",
            error_code="LOOKUP_FAILED",
            finished_at=None,
            duration_ms=1,
        )
        write = SimpleNamespace(
            tool_call_id="write",
            tool_name="user.create",
            readonly_snapshot=False,
            status="success",
            error_code=None,
            finished_at=None,
            duration_ms=1,
        )
        assert effective_log_state(readonly, states).status == "failed"
        assert effective_log_state(write, states).unknown
        assert (
            effective_call_state(
                {"tool": "user.lookup", "tool_call_id": "read"}, states
            )
            is None
        )
        assert effective_call_state(
            {"tool": "user.create", "tool_call_id": "write"}, states
        ).unknown
        assert db.info[authorization_owner.QUARANTINE_KEY]
        assert id(db) in authorization_owner._QUARANTINED
    finally:
        gate.set()
        entry = authorization_owner._QUARANTINED.get(id(db))
        if entry is not None:
            await entry[2]


async def test_effective_trace_filter_counts_before_paginating(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    await approve(case)
    owned_ids = []
    agent = f"read_{case.tool_call_id}"
    try:
        async with AsyncSessionLocal() as db, db.begin():
            action = await db.get(AiPreparedAction, case.action_id)
            original = await db.get(AiOperationLog, case.log_id)
            action.agent_code = original.agent_code = agent
            for index, (status, readonly) in enumerate(
                [("failed", False), ("success", True), ("success", False)], start=1
            ):
                values = {
                    column.name: getattr(original, column.name)
                    for column in AiOperationLog.__table__.columns
                    if column.name != "log_id"
                }
                values.update(
                    trace_id=f"{case.tool_call_id}_{index}",
                    tool_call_id=f"{case.tool_call_id}_{index}",
                    status=status,
                    readonly_snapshot=readonly,
                    queued_at=original.queued_at + timedelta(seconds=index),
                )
                log = AiOperationLog(**values)
                db.add(log)
                await db.flush()
                owned_ids.append(log.log_id)
        async with AsyncSessionLocal() as db:
            pages = [
                await trace_service.list_traces(
                    db,
                    tenant=case.tenant,
                    query=TraceListQuery(
                        agent_code=agent, status="running", current=current, size=1
                    ),
                )
                for current in (1, 2, 3)
            ]
            assert [page.total for page in pages] == [2, 2, 2]
            assert [page.records[0].trace_id for page in pages[:2]] == [
                f"{case.tool_call_id}_3",
                f"{case.tool_call_id}_1",
            ]
            assert pages[2].records == []
            success = await trace_service.list_traces(
                db,
                tenant=case.tenant,
                query=TraceListQuery(agent_code=agent, status="success"),
            )
            assert success.total == 2
            assert all(item.statuses == ["success"] for item in success.records)
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                delete(AiOperationLog).where(AiOperationLog.log_id.in_(owned_ids))
            )
