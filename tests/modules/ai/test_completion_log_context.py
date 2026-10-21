"""New logs and pending actions must be serialized with context deletion."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from tenant_helpers import tenant_context

from app.core.exceptions import BusinessRuleException
from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.hitl.constants import AiExecutionMode, ConfirmAction
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.system.models.user import User


@pytest.fixture
async def context_case(db_session):
    assert db_session.bind is not None
    marker = uuid4().hex
    async with AsyncSessionLocal() as db, db.begin():
        user_id = await db.scalar(
            select(User.user_id).where(User.tenant_id == 0, User.user_name == "admin")
        )
        assert user_id is not None
        conversation = AiConversation(tenant_id=0, user_id=user_id, title=marker)
        db.add(conversation)
        await db.flush()
        source = AiMessage(
            tenant_id=0,
            conversation_id=conversation.conversation_id,
            role="user",
            trace_id=marker,
            content="Create a user",
            is_active=True,
        )
        db.add(source)
        await db.flush()
        tenant = tenant_context(tenant_id=0, actor_user_id=user_id)
        deps = SimpleNamespace(
            trace_id=marker,
            conversation_id=conversation.conversation_id,
            source_user_message_id=source.message_id,
            tenant=tenant,
            user=SimpleNamespace(user_id=user_id),
            agent=SimpleNamespace(code="user_mgmt"),
            injection_hit=False,
            guard_owner_token=None,
            guard_handoff=False,
            command_action="send",
            resolved_model_id=1,
            resolved_provider_id=1,
            data_scope_hash=None,
            projection_dependency_message_ids=(),
            signal_event=None,
        )
    case = SimpleNamespace(
        deps=deps,
        conversation_id=conversation.conversation_id,
        source_id=source.message_id,
        trace_id=marker,
        tenant=tenant,
    )
    try:
        yield case
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            for model in (AiPreparedAction, AiOperationLog):
                await db.execute(
                    delete(model).where(model.tenant_id == 0, model.trace_id == marker)
                )
            await db.execute(
                delete(AiConversation).where(
                    AiConversation.tenant_id == 0,
                    AiConversation.conversation_id == case.conversation_id,
                )
            )


def registered(*, readonly=False):
    return SimpleNamespace(
        meta=AiToolMeta(
            name="user.create",
            agent="user_mgmt",
            summary="Create user",
            required_perms=(),
            risk="high",
            readonly=readonly,
        )
    )


async def start(case, *, readonly=False):
    return await executor._start_log(
        case.deps,
        registered(readonly=readonly),
        f"tc_{uuid4().hex}",
        "a" * 64,
        "Create user",
        AiExecutionMode.HITL,
    )


async def count_logs(case):
    async with AsyncSessionLocal() as db:
        return await db.scalar(
            select(func.count())
            .select_from(AiOperationLog)
            .where(
                AiOperationLog.tenant_id == 0,
                AiOperationLog.trace_id == case.trace_id,
            )
        )


@pytest.mark.parametrize(
    "invalid",
    ["hard_delete", "soft_delete", "inactive", "trace", "missing_id", "bool_id"],
)
async def test_start_log_rejects_invalid_live_context_without_orphan(
    context_case, invalid
):
    case = context_case
    async with AsyncSessionLocal() as db, db.begin():
        if invalid == "hard_delete":
            await db.execute(
                delete(AiConversation).where(
                    AiConversation.conversation_id == case.conversation_id
                )
            )
        elif invalid == "soft_delete":
            await db.execute(
                update(AiConversation)
                .where(AiConversation.conversation_id == case.conversation_id)
                .values(deleted_at=datetime.now(UTC))
            )
        elif invalid == "inactive":
            await db.execute(
                update(AiMessage)
                .where(AiMessage.message_id == case.source_id)
                .values(is_active=False)
            )
        elif invalid == "trace":
            await db.execute(
                update(AiMessage)
                .where(AiMessage.message_id == case.source_id)
                .values(trace_id="another-run")
            )
        elif invalid == "missing_id":
            case.deps.source_user_message_id = None
        else:
            case.deps.conversation_id = True
    with pytest.raises(executor.LogWriteError):
        await start(case)
    assert await count_logs(case) == 0


@pytest.mark.parametrize("readonly", [False, True])
async def test_start_log_accepts_bound_live_context(context_case, readonly):
    case = context_case
    log_id = await start(case, readonly=readonly)
    async with AsyncSessionLocal() as db:
        log = await db.get(AiOperationLog, log_id)
        assert log.source_user_message_id == case.source_id
        assert log.conversation_id == case.conversation_id
        assert log.readonly_snapshot is readonly


async def test_pending_inserts_pa_before_taking_log_lock(context_case, monkeypatch):
    case = context_case
    log_id = await start(case)
    confirmation_id = f"cid_{uuid4().hex}"
    payload = SimpleNamespace(
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    )
    monkeypatch.setattr(
        executor.hitl_manager, "generate_confirmation_id", lambda: confirmation_id
    )
    monkeypatch.setattr(
        executor.hitl_manager, "create_pending", AsyncMock(return_value=payload)
    )
    monkeypatch.setattr(
        executor.hitl_manager, "bind_durable_action", AsyncMock(return_value=payload)
    )
    monkeypatch.setattr(
        executor.hitl_manager, "hang", AsyncMock(return_value=ConfirmAction.APPROVED)
    )
    monkeypatch.setattr(executor, "_rollback_failed_confirmation_setup", AsyncMock())
    original_attach = operation_log_service.attach_confirmation
    observed_actions = []

    async def attach(db, *args, **kwargs):
        observed_actions.append(
            await db.scalar(
                select(AiPreparedAction.action_id).where(
                    AiPreparedAction.tenant_id == case.tenant.tenant_id,
                    AiPreparedAction.confirmation_id == confirmation_id,
                )
            )
        )
        return await original_attach(db, *args, **kwargs)

    monkeypatch.setattr(operation_log_service, "attach_confirmation", attach)
    async with AsyncSessionLocal() as db:
        log = await db.get(AiOperationLog, log_id)
        tool_call_id = log.tool_call_id
    await executor._hang_for_confirmation(
        case.deps,
        registered(),
        log_id,
        tool_call_id,
        {"user_name": "new-user"},
        "Create user",
        None,
    )
    assert len(observed_actions) == 1 and observed_actions[0] is not None


async def test_log_reference_change_after_plan_is_rejected(context_case, monkeypatch):
    case = context_case
    log_id = await start(case)
    async with AsyncSessionLocal() as db, db.begin():
        original_execute = db.execute
        first = True

        async def execute(statement, *args, **kwargs):
            nonlocal first
            result = await original_execute(statement, *args, **kwargs)
            if first:
                first = False
                await original_execute(
                    update(AiOperationLog)
                    .where(
                        AiOperationLog.log_id == log_id,
                    )
                    .values(source_user_message_id=case.source_id + 1)
                )
            return result

        monkeypatch.setattr(db, "execute", execute)
        with pytest.raises(BusinessRuleException, match="绑定"):
            await operation_log_service._get(db, log_id, tenant=case.tenant)


async def test_pending_rejects_foreign_log_context_before_creating_action(
    context_case, monkeypatch
):
    case = context_case
    log_id = await start(case)
    confirmation_id = f"cid_{uuid4().hex}"
    payload = SimpleNamespace(
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    )
    monkeypatch.setattr(
        executor.hitl_manager, "generate_confirmation_id", lambda: confirmation_id
    )
    monkeypatch.setattr(
        executor.hitl_manager, "create_pending", AsyncMock(return_value=payload)
    )
    monkeypatch.setattr(
        executor.hitl_manager, "bind_durable_action", AsyncMock(return_value=payload)
    )
    monkeypatch.setattr(
        executor.hitl_manager, "hang", AsyncMock(return_value=ConfirmAction.APPROVED)
    )
    monkeypatch.setattr(executor, "_rollback_failed_confirmation_setup", AsyncMock())
    async with AsyncSessionLocal() as db, db.begin():
        log = await db.get(AiOperationLog, log_id)
        tool_call_id = log.tool_call_id
        log.source_user_message_id = case.source_id + 1
    with pytest.raises(BusinessRuleException, match="绑定"):
        await executor._hang_for_confirmation(
            case.deps,
            registered(),
            log_id,
            tool_call_id,
            {"user_name": "new-user"},
            "Create user",
            None,
        )
    async with AsyncSessionLocal() as db:
        assert (
            await db.scalar(
                select(AiPreparedAction.action_id).where(
                    AiPreparedAction.confirmation_id == confirmation_id,
                    AiPreparedAction.tenant_id == 0,
                )
            )
            is None
        )
        log = await db.get(AiOperationLog, log_id)
        assert log.confirmation_id is None


async def test_start_log_waits_for_delete_lock_then_rejects_without_orphan(
    context_case, monkeypatch
):
    case = context_case
    writer_entered = asyncio.Event()
    writer_pid = None
    original_lock = prepared_action_service.lock_source_binding

    async def track_lock(db, **kwargs):
        nonlocal writer_pid
        writer_pid = await db.scalar(select(func.pg_backend_pid()))
        writer_entered.set()
        return await original_lock(db, **kwargs)

    monkeypatch.setattr(prepared_action_service, "lock_source_binding", track_lock)
    task = None
    async with AsyncSessionLocal() as deleter:
        await deleter.begin()
        await deleter.execute(
            delete(AiConversation).where(
                AiConversation.tenant_id == 0,
                AiConversation.conversation_id == case.conversation_id,
            )
        )
        try:
            task = asyncio.create_task(start(case))
            await asyncio.wait_for(writer_entered.wait(), timeout=1)
            # Observe a real PostgreSQL row-lock wait, not a timed task sleep.
            from sqlalchemy import text  # noqa: PLC0415

            async with AsyncSessionLocal() as observer, asyncio.timeout(1):
                while True:
                    await observer.execute(select(func.pg_stat_clear_snapshot()))
                    blocked = await observer.scalar(
                        text(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
                        ),
                        {"pid": writer_pid},
                    )
                    if blocked:
                        break
                    await asyncio.sleep(0.01)
            assert not task.done()
            await deleter.commit()
            with pytest.raises(executor.LogWriteError):
                await asyncio.wait_for(task, timeout=2)
            assert await count_logs(case) == 0
        finally:
            await deleter.rollback()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
