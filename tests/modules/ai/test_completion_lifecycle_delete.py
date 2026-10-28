"""Recovery and soft deletion share the completion context lock order."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event

from app.core import redis as redis_module
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai import lifecycle
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.api import confirm, resume
from app.modules.ai.api import conversation as conversation_api
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.chat_run_service import chat_run_finalizer
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.auth.service import load_live_user_authority
from tests.modules.ai.test_atomic_completion_guards import PLATFORM, TENANT
from tests.modules.ai.test_atomic_prepared_completion import approve, count_real_handler
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_completion_resume_guards import leave_claim_running
from tests.modules.ai.test_resume import _make_pending


@pytest.mark.parametrize(
    ("tool", "owner", "status", "expected"),
    [
        ("user.list", None, "succeeded", True),
        ("user.list", None, "failed", True),
        ("user.list", "v0162-db:invalid", "failed", False),
        ("unknown.tool", None, "succeeded", False),
        ("user.create", None, "failed", False),
    ],
)
async def test_registered_readonly_cache_terminal_compatibility(
    completion_case, tool, owner, status, expected
):
    case = completion_case
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        action.execute_tool_name = tool
        action.status = status
        action.execution_owner = owner
        action.execution_lease_expires_at = None
        action.row_version = 4
        action.finished_at = now
        action.error_code = (
            "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED" if status == "failed" else None
        )
        log.tool_name = tool
        log.readonly_snapshot = tool == "user.list"
        log.status = "failed" if status == "failed" else "success"
        log.finished_at = now.replace(tzinfo=None)
        log.started_at = now.replace(tzinfo=None)
        log.error_code = action.error_code
    result = await lifecycle.get_authorization_runtime().recovery.observe_cached(
        tenant_id=case.tenant.tenant_id,
        user_id=case.tenant.actor_user_id,
        confirmation_id=case.confirmation_id,
        redis=AsyncMock(),
    )
    assert (result.disposition != "unknown") is expected


@pytest.mark.parametrize(
    ("tool", "owner", "expired", "periodic"),
    [
        ("user.list", "readonly-worker", True, False),
        ("user.list", "readonly-worker", True, True),
        ("user.list", "readonly-worker", False, False),
        ("user.list", "v0162-db:invalid", True, False),
        ("unknown.tool", "legacy-worker", True, False),
        ("user.create", "legacy-worker", True, False),
    ],
)
async def test_cleanup_only_recovers_expired_registered_readonly_claim(
    completion_case, tool, owner, expired, periodic
):
    case = completion_case
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        action.execute_tool_name = tool
        action.status = "running"
        action.row_version = 2
        action.execution_owner = owner
        action.execution_lease_expires_at = datetime.now(UTC) + timedelta(
            seconds=-1 if expired else 60
        )
        log = await db.get(AiOperationLog, case.log_id)
        log.tool_name = tool
        log.readonly_snapshot = tool == "user.list"
        log.status = "running"
        log.started_at = datetime.now(UTC).replace(tzinfo=None)
    await lifecycle.cleanup_prepared_actions_on_startup(
        redis_module.redis_client, platform=PLATFORM, expired_only=periodic
    )
    expected = (
        "failed"
        if tool == "user.list" and owner == "readonly-worker" and expired
        else "running"
    )
    async with AsyncSessionLocal() as db:
        assert (await db.get(AiPreparedAction, case.action_id)).status == expected
        assert (await db.get(AiOperationLog, case.log_id)).status == expected
    assert case.calls == []


@pytest.mark.parametrize(
    "status", ["running", "success", "failed", "rejected", "expired"]
)
async def test_legacy_write_orphan_never_synthesizes_completion(monkeypatch, status):
    db = AsyncMock()
    db.scalar.return_value = None
    finalize, delete = AsyncMock(), AsyncMock()
    monkeypatch.setattr(chat_run_finalizer, "finalize_pending_turn", finalize)
    monkeypatch.setattr(lifecycle.hitl_manager, "delete_pending", delete)
    await lifecycle.finalize_orphaned_pending(
        db,
        MagicMock(),
        confirmation_id="cid_legacy_unknown",
        pending=_make_pending(),
        operation_log=SimpleNamespace(
            status=status,
            readonly_snapshot=False,
            tool_name="user.create",
            error_code=None,
            result_summary="unproven",
        ),
        tenant=TENANT,
    )
    finalize.assert_not_awaited()
    delete.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_delete_timeout_never_waits_for_stalled_cleanup(monkeypatch):
    release = asyncio.Event()
    db = MagicMock(info={})
    db.execute = AsyncMock(side_effect=TimeoutError("Injected lock timeout"))
    db.invalidate = AsyncMock(side_effect=release.wait)
    db.rollback = AsyncMock(side_effect=release.wait)
    monkeypatch.setattr(conversation_api, "get_bound_tenant_context", lambda _: TENANT)
    task = asyncio.create_task(
        conversation_api.delete_conversation(
            123, db, SimpleNamespace(user_id=TENANT.actor_user_id)
        )
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=1.5)
        assert task in done, "Connection cleanup must not extend the request deadline"
        assert task.exception().error_code == "AI_ACTION_RUNNING"
        db.rollback.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    "state",
    [
        "succeeded",
        "corrupted_success",
        "db_failed",
        "file_failed",
        "legacy_failed",
        "rejected",
        "rejected_wrong_version",
        "expired",
        "expired_started",
    ],
)
async def test_cache_sweep_clears_only_verified_completion(
    completion_case, monkeypatch, state
):
    case = completion_case
    if state.startswith("rejected"):
        async with AsyncSessionLocal() as db:
            actor = await load_live_user_authority(db, tenant=case.tenant)
            await confirm.confirm_tool(
                confirm.ConfirmRequest(
                    confirmation_id=case.confirmation_id, action="reject"
                ),
                db,
                actor,
            )
    elif state.startswith("expired"):
        async with AsyncSessionLocal() as db, db.begin():
            action = await db.get(AiPreparedAction, case.action_id)
            action.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await lifecycle.cleanup_prepared_actions_on_startup(
            redis_module.redis_client, platform=PLATFORM, expired_only=True
        )
    else:
        count_real_handler(case, monkeypatch, return_failure=state.endswith("failed"))
        await approve(case)
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        if state == "corrupted_success":
            log.args_hash = "f" * 64
        elif state == "file_failed":
            action.execute_tool_name = log.tool_name = "user.export"
            action.execution_owner = action.execution_owner.replace("-db:", "-file:")
        elif state == "legacy_failed":
            action.execution_owner = None
        elif state == "rejected_wrong_version":
            action.row_version = 4
        elif state == "expired_started":
            log.started_at = datetime.now(UTC).replace(tzinfo=None)
        pending = resume._pending_from_durable_action(action)

    async def keys(**_kwargs):
        yield f"ai:confirm:tenant:{case.tenant.tenant_id}:{case.confirmation_id}"

    monkeypatch.setattr(redis_module.redis_client, "scan_iter", keys)
    monkeypatch.setattr(
        lifecycle, "cleanup_prepared_actions_on_startup", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        lifecycle.hitl_manager,
        "get_pending_for_platform",
        AsyncMock(return_value=pending),
    )
    delete = AsyncMock()
    monkeypatch.setattr(lifecycle.hitl_manager, "delete_pending", delete)
    await lifecycle.cleanup_orphaned_pending_on_startup(platform=PLATFORM)
    assert delete.await_count == int(
        state in {"succeeded", "db_failed", "rejected", "expired"}
    )


@pytest.mark.parametrize("periodic", [False, True])
async def test_cleanup_recovers_expired_db_claim_without_execution(
    completion_case, monkeypatch, periodic
):
    case = completion_case
    await leave_claim_running(case, monkeypatch)
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        action.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await lifecycle.cleanup_prepared_actions_on_startup(
        redis_module.redis_client, platform=PLATFORM, expired_only=periodic
    )
    async with AsyncSessionLocal() as db:
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        assert action.status == log.status == "failed"
        assert action.execution_owner.startswith("v0162-db:")
        assert action.execution_lease_expires_at is None
    assert case.calls == []


async def test_startup_preserves_unproven_approved_fact(completion_case):
    case = completion_case
    async with AsyncSessionLocal() as db, db.begin():
        action = await db.get(AiPreparedAction, case.action_id)
        action.status = "approved"
        action.approved_by = case.tenant.actor_user_id
        action.approved_at = datetime.now(UTC)
    await lifecycle.cleanup_prepared_actions_on_startup(
        redis_module.redis_client, platform=PLATFORM
    )
    async with AsyncSessionLocal() as db:
        assert (await db.get(AiPreparedAction, case.action_id)).status == "approved"
        assert (
            await db.get(AiOperationLog, case.log_id)
        ).status == "pending_confirmation"
    assert case.calls == []
    async with AsyncSessionLocal() as db:
        actor = await load_live_user_authority(db, tenant=case.tenant)
        with pytest.raises(Exception, match="核对"):
            await conversation_api.delete_conversation(case.conversation_id, db, actor)


async def test_soft_delete_locks_sources_before_actions(completion_case):
    case = completion_case
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement:
            statements.append(statement)

    async with AsyncSessionLocal() as db:
        actor = await load_live_user_authority(db, tenant=case.tenant)
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            await conversation_api.delete_conversation(case.conversation_id, db, actor)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
    tables = [
        next(
            (
                name
                for name in (
                    "ai_conversation",
                    "ai_message",
                    "ai_prepared_action",
                    "ai_operation_log",
                )
                if f"FROM {name}" in sql
            ),
            "other",
        )
        for sql in statements
    ]
    assert tables.index("ai_conversation") < tables.index("ai_message")
    assert tables.index("ai_message") < tables.index("ai_prepared_action")
    assert tables.index("ai_prepared_action") < tables.index("ai_operation_log")


async def test_soft_delete_does_not_trust_stale_context(completion_case):
    case = completion_case
    async with AsyncSessionLocal() as stale_db:
        conversation = await conversation_service.get_by_id(
            stale_db,
            case.conversation_id,
            case.tenant.actor_user_id,
            tenant=case.tenant,
        )
        async with AsyncSessionLocal() as db, db.begin():
            current = await conversation_service.get_by_id(
                db, case.conversation_id, case.tenant.actor_user_id, tenant=case.tenant
            )
            current.title = "fresh locked title"
        locked = await conversation_service.lock_for_delete(
            stale_db,
            case.conversation_id,
            case.tenant.actor_user_id,
            tenant=case.tenant,
        )
        assert locked is conversation
        assert locked.title == "fresh locked title"


async def test_soft_delete_rejects_corrupted_success_proof(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    await approve(case)
    async with AsyncSessionLocal() as db, db.begin():
        (await db.get(AiOperationLog, case.log_id)).args_hash = "f" * 64
    async with AsyncSessionLocal() as db:
        actor = await load_live_user_authority(db, tenant=case.tenant)
        with pytest.raises(Exception, match="核对|执行|完成"):
            await conversation_api.delete_conversation(case.conversation_id, db, actor)


@pytest.mark.parametrize(
    ("status", "readonly"),
    [
        ("success", False),
        ("failed", False),
        ("rejected", False),
        ("expired", False),
        ("success", True),
    ],
)
async def test_soft_delete_rejects_unproven_write_orphan(
    completion_case, monkeypatch, status, readonly
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    await approve(case)
    async with AsyncSessionLocal() as db, db.begin():
        await db.delete(await db.get(AiPreparedAction, case.action_id))
        log = await db.get(AiOperationLog, case.log_id)
        log.status, log.readonly_snapshot = status, readonly
    async with AsyncSessionLocal() as db:
        actor = await load_live_user_authority(db, tenant=case.tenant)
        if readonly:
            await conversation_api.delete_conversation(case.conversation_id, db, actor)
        else:
            with pytest.raises(Exception, match="核对"):
                await conversation_api.delete_conversation(
                    case.conversation_id, db, actor
                )


@pytest.mark.parametrize("contender", ["cleanup", "delete", "stream"])
async def test_business_lock_outlives_lease_and_serializes_bypass(
    completion_case, monkeypatch, contender
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    registered = ToolRegistry.get().find("user.create")
    actual = registered.fn
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_handler(ctx, **args):
        result = await actual(ctx, **args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(registered, "fn", held_handler)
    monkeypatch.setattr(confirm, "_EXECUTION_LEASE_TTL", timedelta(milliseconds=500))

    async def contend():
        if contender == "cleanup":
            return await lifecycle.cleanup_prepared_actions_on_startup(
                redis_module.redis_client, platform=PLATFORM, expired_only=True
            )
        async with AsyncSessionLocal() as db:
            if contender == "delete":
                actor = await load_live_user_authority(db, tenant=case.tenant)
                return await conversation_api.delete_conversation(
                    case.conversation_id, db, actor
                )
            action = await db.get(AiPreparedAction, case.action_id)
            await chat_run_finalizer.finalize_assistant_turn(
                db,
                conversation_id=case.conversation_id,
                trace_id=action.trace_id,
                source_user_message_id=action.source_user_message_id,
                content="",
                agent_code=action.agent_code,
                tenant=case.tenant,
                tool_calls=[
                    {
                        "tool": "user.create",
                        "tool_call_id": case.tool_call_id,
                        "ok": False,
                        "error_code": "AI_INTERNAL_ERROR",
                        "duration_ms": 0,
                    }
                ],
            )
            await db.commit()

    approval = asyncio.create_task(approve(case))
    bypass = None
    try:
        await asyncio.wait_for(entered.wait(), 4)
        await asyncio.sleep(0.6)
        bypass = asyncio.create_task(contend())
        await asyncio.sleep(0.1)
        assert not bypass.done(), (
            "The competing path must wait for the business context lock"
        )
        release.set()
        assert (await asyncio.wait_for(approval, 4)).data.status == "succeeded"
        await asyncio.wait_for(bypass, 4)
    finally:
        release.set()
        await asyncio.gather(
            approval, *([bypass] if bypass is not None else []), return_exceptions=True
        )
    async with AsyncSessionLocal() as db:
        assert (await db.get(AiPreparedAction, case.action_id)).status == "succeeded"
        assert (await db.get(AiOperationLog, case.log_id)).status == "success"
    assert len(case.calls) == 1


async def test_soft_delete_checks_all_actions_before_expiring_any(
    completion_case, monkeypatch
):
    case = completion_case
    await leave_claim_running(case, monkeypatch)
    duplicate_id = None
    try:
        async with AsyncSessionLocal() as db, db.begin():
            running = await db.get(AiPreparedAction, case.action_id)
            fields = {
                column.key: getattr(running, column.key)
                for column in AiPreparedAction.__table__.columns
                if not column.primary_key
            }
            fields.update(
                confirmation_id=case.confirmation_id + "_pending",
                execute_tool_call_id=case.tool_call_id + "_pending",
                status="pending_confirmation",
                execution_owner=None,
                execution_lease_expires_at=None,
            )
            pending = AiPreparedAction(**fields)
            db.add(pending)
            await db.flush()
            duplicate_id = pending.action_id
        async with AsyncSessionLocal() as db:
            actor = await load_live_user_authority(db, tenant=case.tenant)
            with pytest.raises(Exception, match="核对"):
                await conversation_api.delete_conversation(
                    case.conversation_id, db, actor
                )
        async with AsyncSessionLocal() as db:
            assert (
                await db.get(AiPreparedAction, duplicate_id)
            ).status == "pending_confirmation"
            assert (await db.get(AiPreparedAction, case.action_id)).status == "running"
    finally:
        if duplicate_id is not None:
            async with AsyncSessionLocal() as db, db.begin():
                await db.delete(await db.get(AiPreparedAction, duplicate_id))
