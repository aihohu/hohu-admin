"""New completion owners survive legacy cleanup and stream failure paths."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, update
from tenant_helpers import tenant_context

from app.core import redis as redis_module
from app.core.exceptions import BusinessRuleException
from app.core.tenant import PlatformContext
from app.db.authorization_ai_owner import AIAuthorizationOwner
from app.modules.ai import lifecycle
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.hitl.events import AiErrorEvent, DoneEvent
from app.modules.ai.api import chat, resume
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.authorization_lifecycle_candidate import lifecycle_candidate
from app.modules.ai.service.chat_run_service import chat_run_finalizer, chat_run_guard
from tests.modules.ai.test_authorization_background_recovery import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_background_recovery import (
    pending_case as pending_case,
)
from tests.modules.ai.test_authorization_background_recovery import (
    plan_sessions as plan_sessions,
)
from tests.modules.ai.test_authorization_background_recovery import (
    recovery_case as recovery_case,
)
from tests.modules.ai.test_authorization_background_recovery import (
    system_sessions as system_sessions,
)

TENANT = tenant_context(actor_user_id=103)
PLATFORM = PlatformContext(
    actor_principal_id=0,
    actor_name="completion-guard-test",
    principal_type="service",
    permissions=frozenset({"platform:ai:lifecycle"}),
    reason="completion guard test",
    ticket_id="TEST-COMPLETION-GUARD",
    correlation_id="v0162",
)


def _action(
    *,
    tool="user.create",
    owner="v0162-db:00000000-0000-0000-0000-000000000001",
    lease=None,
):
    return SimpleNamespace(
        action_id=9001,
        confirmation_id="cid_atomic_guard",
        status="running",
        execution_owner=owner,
        execution_lease_expires_at=lease,
        execute_tool_name=tool,
        execute_tool_call_id="tc_atomic_guard",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        guard_owner_token="guard-owner",
        conversation_id=100,
        source_user_message_id=101,
        tenant_id=0,
        user_id=103,
        row_version=3,
        duration_ms=None,
        error_code=None,
    )


def _session_factory(db):
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db)
    factory.return_value.__aexit__ = AsyncMock(return_value=None)
    db.begin.return_value.__aenter__ = AsyncMock()
    db.begin.return_value.__aexit__ = AsyncMock()
    return factory


@pytest.mark.parametrize(
    ("tool", "owner"),
    [
        ("user.create", "v0162-db:00000000-0000-0000-0000-000000000001"),
        ("user.export", "v0162-file:00000000-0000-0000-0000-000000000001"),
        ("user.create", "v0162-db:invalid-owner"),
        ("user.create", "v0162-file:00000000-0000-0000-0000-000000000001"),
    ],
)
@pytest.mark.parametrize("expired_lease", [False, True])
@pytest.mark.parametrize("late_claim", [False, True])
async def test_startup_preserves_protocol_running_without_live_lease(
    pending_case, monkeypatch, tool, owner, expired_lease, late_claim
):
    factory, plain, _ = pending_case
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                execute_tool_name=tool,
                execution_owner=owner,
                status="approved" if late_claim else "running",
                row_version=3,
                approved_by=101,
                approved_at=datetime.now(UTC),
                execution_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
                if expired_lease
                else None,
            )
        )
        # No positive paired log proof: a protocol-looking marker is insufficient.
        await db.execute(update(AiOperationLog).values(args_hash="f" * 64))
        await db.commit()
    original = lifecycle_candidate.reference
    seen = False

    async def claimed(*args, **kwargs):
        nonlocal seen
        reference = await original(*args, **kwargs)
        if late_claim and not seen:
            seen = True
            async with plain() as db:
                await db.execute(update(AiPreparedAction).values(status="running"))
                await db.commit()
        return reference

    monkeypatch.setattr(lifecycle_candidate, "reference", claimed)
    monkeypatch.setattr(
        lifecycle,
        "get_authorization_runtime",
        lambda: SimpleNamespace(recovery=AIAuthorizationOwner(factory)),
    )
    monkeypatch.setattr(lifecycle, "_recovery_after", None)
    monkeypatch.setattr(lifecycle, "_recovery_startup_before", None)
    release, delete = AsyncMock(), AsyncMock()
    monkeypatch.setattr(chat_run_guard, "release", release)
    monkeypatch.setattr(lifecycle.hitl_manager, "delete_pending", delete)
    assert (
        await lifecycle.cleanup_prepared_actions_on_startup(
            AsyncMock(), platform=PLATFORM
        )
        == 0
    )
    async with plain() as db:
        assert await db.scalar(select(AiPreparedAction.status)) == "running"
        assert await db.scalar(select(AiOperationLog.status)) == "pending_confirmation"
        assert (
            await db.scalar(
                select(AiMessage.message_id).where(AiMessage.role == "assistant")
            )
            is None
        )
    release.assert_not_awaited()
    delete.assert_not_awaited()


async def test_redis_sweep_preserves_protocol_running_pending(
    pending_case, monkeypatch
):
    factory, plain, case = pending_case
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                status="running",
                row_version=3,
                execution_owner=case.claim.execution_owner,
                execution_lease_expires_at=datetime.now(UTC) + timedelta(seconds=45),
            )
        )
        await db.commit()
    redis = AsyncMock()

    async def keys(**_kwargs):
        yield f"ai:confirm:tenant:0:{case.action.confirmation_id}"

    redis.scan_iter = keys
    monkeypatch.setattr(redis_module, "redis_client", redis)
    monkeypatch.setattr(
        lifecycle,
        "get_authorization_runtime",
        lambda: SimpleNamespace(recovery=AIAuthorizationOwner(factory)),
    )
    monkeypatch.setattr(
        lifecycle, "cleanup_prepared_actions_on_startup", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        lifecycle.hitl_manager,
        "get_pending_for_platform",
        AsyncMock(
            return_value=SimpleNamespace(
                tenant_id=0, user_id=101, action_id=case.action.action_id
            )
        ),
    )
    delete = AsyncMock()
    monkeypatch.setattr(lifecycle.hitl_manager, "delete_pending", delete)
    assert await lifecycle.cleanup_orphaned_pending_on_startup(platform=PLATFORM) == 0
    delete.assert_not_awaited()
    async with plain() as db:
        assert await db.scalar(select(AiPreparedAction.status)) == "running"


async def test_legacy_finalizer_rejects_durable_pending_payload(monkeypatch):
    finalize = AsyncMock()
    monkeypatch.setattr(chat_run_finalizer, "finalize_pending_turn", finalize)
    db = AsyncMock()
    await lifecycle.finalize_orphaned_pending(
        db,
        MagicMock(),
        confirmation_id="cid_atomic_guard",
        pending=SimpleNamespace(action_id=9001),
        operation_log=None,
        tenant=TENANT,
    )
    finalize.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "owner",
    ["v0162-db:00000000-0000-0000-0000-000000000001", "v0162-db:invalid-owner"],
)
async def test_terminal_loader_stops_agent_for_protocol_running(monkeypatch, owner):
    monkeypatch.setattr(
        executor,
        "_get_prepared_action_by_confirmation",
        AsyncMock(return_value=_action(owner=owner)),
    )
    with pytest.raises(BusinessRuleException) as raised:
        await executor._load_prepared_terminal_result("cid_atomic_guard", tenant=TENANT)
    assert raised.value.error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"


@pytest.mark.parametrize("missing", [False, True])
async def test_durable_waiter_cannot_infer_failure_from_unavailable_fact(
    monkeypatch, missing
):
    db = MagicMock()
    monkeypatch.setattr(executor, "AsyncSessionLocal", _session_factory(db))
    reader = AsyncMock(return_value=None)
    if not missing:
        reader.side_effect = ConnectionError("primary unavailable")
    monkeypatch.setattr(
        executor.prepared_action_service, "get_by_confirmation_id", reader
    )
    with pytest.raises(BusinessRuleException) as raised:
        await executor._load_prepared_terminal_result("cid_atomic_guard", tenant=TENANT)
    assert raised.value.error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"


async def test_resume_reports_unknown_without_failed_persistence(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(resume, "AsyncSessionLocal", _session_factory(db))
    monkeypatch.setattr(
        resume, "_durable_binding_valid", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        resume.prepared_action_service,
        "get_by_confirmation_id",
        AsyncMock(return_value=_action()),
    )
    events = await resume._load_durable_resume_terminal(
        confirmation_id="cid_atomic_guard",
        pending=SimpleNamespace(action_id=9001, trace_id="tr_atomic_guard"),
        user_id=103,
        tenant=TENANT,
    )
    assert isinstance(events[0], AiErrorEvent)
    assert events[0].error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
    assert "请勿重新办理" in events[0].message
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].persistence == "not_applicable"


async def test_resume_unavailable_primary_does_not_report_failed_execution(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(resume, "AsyncSessionLocal", _session_factory(db))
    monkeypatch.setattr(
        resume.prepared_action_service,
        "get_by_confirmation_id",
        AsyncMock(side_effect=ConnectionError("primary unavailable")),
    )
    events = await resume._load_durable_resume_terminal(
        confirmation_id="cid_atomic_guard",
        pending=SimpleNamespace(action_id=9001, trace_id="tr_atomic_guard"),
        user_id=103,
        tenant=TENANT,
    )
    assert events[0].error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
    assert events[-1].persistence == "not_applicable"


async def test_stream_persists_unknown_message_without_retry_advice(monkeypatch):
    db = AsyncMock()
    finalize = AsyncMock(return_value=SimpleNamespace(message_id=105))
    monkeypatch.setattr(chat.chat_run_finalizer, "finalize_assistant_turn", finalize)
    await chat._finalize_stream_turn(
        db,
        conversation_id=100,
        trace_id="tr_atomic_guard",
        source_user_message_id=101,
        content="untrusted model output",
        tool_calls=None,
        agent_code=None,
        stream_error_code="AI_EXECUTION_OUTCOME_UNKNOWN",
        tenant=TENANT,
    )
    content = finalize.await_args.kwargs["content"]
    assert "请勿重新办理" in content
    assert "可以重新提问" not in content
    db.commit.assert_awaited_once()


@pytest.mark.parametrize("status", ["running", "succeeded"])
async def test_fallback_log_finalizer_defers_to_durable_context(monkeypatch, status):
    action = _action()
    action.status = status
    if status == "succeeded":
        action.execution_owner = None
    calls = []
    db = MagicMock()

    async def scalar(statement):
        sql = str(statement.compile())
        if "FOR UPDATE" in sql:
            calls.append("log-lock")
            return SimpleNamespace(
                status="success" if status == "succeeded" else "running"
            )
        calls.append("discover")
        return action

    async def lock_context(*_args, **_kwargs):
        calls.append("context-lock")
        return SimpleNamespace(action=action)

    async def retry(_operation, *, log_id, op, raise_on_failure):
        assert log_id == 901
        assert not raise_on_failure
        await op(db)

    db.scalar = AsyncMock(side_effect=scalar)
    monkeypatch.setattr(
        executor.prepared_action_service,
        "lock_confirmation_context",
        AsyncMock(side_effect=lock_context),
    )
    monkeypatch.setattr(executor, "_with_log_retry", retry)
    mark_failed = AsyncMock()
    monkeypatch.setattr(executor.operation_log_service, "mark_failed", mark_failed)

    await executor._finish_log_final(
        901, executor.ToolResult.failure("TEST_FAILURE", "failure"), 0, tenant=TENANT
    )

    assert calls == ["discover", "context-lock", "log-lock"]
    mark_failed.assert_not_awaited()


async def test_fallback_log_writer_reloads_terminal_state_after_lock():
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[None, SimpleNamespace(status="success")])
    assert await executor._log_has_durable_owner(db, 901, tenant=TENANT)
    statement = db.scalar.await_args_list[1].args[0]
    assert "FOR UPDATE" in str(statement.compile())
    assert statement.get_execution_options()["populate_existing"] is True


async def test_durable_resume_cannot_fall_back_to_legacy_handler():
    deps = SimpleNamespace(guard_handoff=False)
    with pytest.raises(BusinessRuleException) as raised:
        await executor.resume_tool_execution(SimpleNamespace(action_id=9001), deps, 901)
    assert raised.value.error_code == "AI_EXECUTION_OUTCOME_UNKNOWN"
    assert deps.guard_handoff is True
