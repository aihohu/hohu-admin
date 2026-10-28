"""Real pending proposals and Redis hints obey fixed owner boundaries."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, insert, select, update

from app.core.exceptions import BusinessException
from app.db import authorization_lifecycle_owner, authorization_pending_owner
from app.db.base import role_menus, user_roles
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.gateway.atomic_execution import (
    PreparedExecutionOutcomeUnknown,
)
from app.modules.ai.agents.gateway.failures import compute_args_hash
from app.modules.ai.agents.hitl.constants import AiExecutionMode
from app.modules.ai.agents.hitl.events import ConfirmationRequiredEvent
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.api import confirm as confirm_module
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.confirm import ConfirmRequest
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.system.models.menu import Menu
from app.modules.system.models.user import User
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_preview_atomic_completion import authenticated_actor
from tests.modules.ai.test_preview_atomic_completion import preview_case as preview_case


async def pending_start(preview):
    tool = ToolRegistry.get().find("user.export")
    args = {"reason": "pending regression"}
    call_id = "tc_" + uuid4().hex
    log_id = await executor._start_log(
        preview.deps,
        tool,
        call_id,
        compute_args_hash(args),
        "tool=user.export",
        AiExecutionMode.HITL,
    )
    return tool, args, call_id, log_id


@pytest.mark.parametrize("approve", [False, True])
async def test_direct_canonical_arguments_bind_original_log(
    preview_case, monkeypatch, approve
):
    username = "canonical" + uuid4().hex[:16]
    async with AsyncSessionLocal() as db, db.begin():
        target = User(tenant_id=0, user_name=username, hashed_password="fixture-unused")
        # This exact-confirmation test explicitly grants the system writer's
        # batch-delete permission as well as the tool's existing delete grant.
        permission = Menu(
            tenant_id=0,
            menu_name=username,
            menu_type="F",
            status="1",
            permission="system:user:batch-delete",
        )
        db.add_all([target, permission])
        await db.flush()
        target_id = target.user_id
        role_id = await db.scalar(
            select(user_roles.c.role_id).where(
                user_roles.c.user_id == preview_case.case.tenant.actor_user_id
            )
        )
        await db.execute(
            insert(role_menus).values(
                tenant_id=0, role_id=role_id, menu_id=permission.menu_id
            )
        )
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(db, tenant=preview_case.case.tenant)
        preview_case.deps.data_scope_hash = (
            await result_projection_service.compute_data_scope_hash(db, actor)
        )
        db.expunge_all()
        preview_case.deps.user = actor

    async def stop(event):
        if isinstance(event, ConfirmationRequiredEvent):
            if approve:
                async with AsyncSessionLocal() as db:
                    actor = await authenticated_actor(
                        db, tenant=preview_case.case.tenant
                    )
                    await confirm_module.confirm_tool(
                        ConfirmRequest(
                            confirmation_id=event.confirmation_id, action="approve"
                        ),
                        db=db,
                        current_user=actor,
                    )
                return
            raise RuntimeError("stop after confirmation boundary")

    preview_case.deps.signal_event = stop
    original = {"user_names": [username]}
    legacy_reads = []
    reader = executor._get_prepared_action_by_confirmation

    async def legacy(*args, **kwargs):
        legacy_reads.append(True)
        return await reader(*args, **kwargs)

    monkeypatch.setattr(executor, "_get_prepared_action_by_confirmation", legacy)
    monkeypatch.setattr(
        executor.hitl_manager, "hang", AsyncMock(side_effect=TimeoutError)
    )
    try:
        if approve:
            result = await executor.execute_tool(
                "user.batch_delete", original, preview_case.deps
            )
            # Deleting the targets removes their current readable projection.
            # The durable success remains intact; no stale target details leak.
            assert result.error_code == "AI_RESULT_PROJECTION_FORBIDDEN"
            assert result.data is None and result.ui is None
            assert legacy_reads == []
        else:
            with pytest.raises(PreparedExecutionOutcomeUnknown):
                await executor.execute_tool(
                    "user.batch_delete", original, preview_case.deps
                )
        async with AsyncSessionLocal() as db:
            action = await db.scalar(
                select(AiPreparedAction).where(
                    AiPreparedAction.conversation_id
                    == preview_case.deps.conversation_id,
                    AiPreparedAction.execute_tool_name == "user.batch_delete",
                )
            )
            log = await db.scalar(
                select(AiOperationLog).where(
                    AiOperationLog.tool_call_id == action.execute_tool_call_id,
                )
            )
            assert (await db.get(User, target_id) is None) == approve
        assert action.frozen_args == {
            "user_ids": [target_id],
            "user_names": None,
            "phones": None,
        }
        assert action.args_hash != compute_args_hash(original)
        assert log.args_hash == action.args_hash
        assert action.status == ("succeeded" if approve else "pending_confirmation")
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(delete(User).where(User.user_id == target_id))
            await db.execute(
                delete(role_menus).where(role_menus.c.menu_id == permission.menu_id)
            )
            await db.execute(delete(Menu).where(Menu.menu_id == permission.menu_id))


async def test_concurrent_pending_creates_one_reserved_action(preview_case):
    tool, args, call_id, log_id = await pending_start(preview_case)
    confirmation_id = await executor._direct_confirmation_id(
        preview_case.deps, tool, log_id, call_id, args
    )
    payload = SimpleNamespace(
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    )

    async def prepare():
        return await executor._prepare_direct_pending(
            preview_case.deps,
            tool,
            log_id,
            call_id,
            args,
            args,
            None,
            payload,
            confirmation_id,
        )

    results = await asyncio.gather(prepare(), prepare(), return_exceptions=True)
    assert sum(isinstance(result, AiPreparedAction) for result in results) == 1
    errors = [result for result in results if isinstance(result, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], BusinessException)
    assert errors[0].error_code in {
        "FIELD_POLICY_LOCK_ORDER",
        "AUTHORIZATION_SNAPSHOT_STALE",
    }
    async with AsyncSessionLocal() as db:
        actions = (
            await db.scalars(
                select(AiPreparedAction).where(
                    AiPreparedAction.tenant_id == 0,
                    AiPreparedAction.execute_tool_call_id == call_id,
                )
            )
        ).all()
        log = await db.get(AiOperationLog, log_id)
    assert len(actions) == 1 and actions[0].confirmation_id == confirmation_id
    assert log.confirmation_id == confirmation_id
    assert preview_case.calls == []


async def test_handoff_final_file_failure_rolls_back_reserved_action(
    preview_case, monkeypatch
):
    created = []
    original_create = prepared_action_service.create_pending
    original_read = preview_case.storage.read_artifact

    async def create(db, **values):
        action = await original_create(db, **values)
        if values["execute_tool_name"] == "user.import_execute":
            created.append(action.action_id)
        return action

    async def read(*args, **kwargs):
        if created:
            raise OSError("injected final handoff FILE failure")
        return await original_read(*args, **kwargs)

    monkeypatch.setattr(prepared_action_service, "create_pending", create)
    monkeypatch.setattr(preview_case.storage, "read_artifact", read)
    preview_case.args["requested_outcome"] = "execute_if_approved"
    with pytest.raises(PreparedExecutionOutcomeUnknown):
        await executor.execute_tool(
            "user.import_preview", preview_case.args, preview_case.deps
        )
    assert len(created) == 1 and len(preview_case.calls) == 1
    async with AsyncSessionLocal() as db:
        assert await db.get(AiPreparedAction, created[0]) is None
        log = await db.scalar(
            select(AiOperationLog).where(
                AiOperationLog.conversation_id == preview_case.deps.conversation_id,
                AiOperationLog.tool_name == "user.import_execute",
            )
        )
    assert log.status == "pending_confirmation" and log.target_summary is None


async def test_gateway_timeout_uses_planned_expiration(preview_case, monkeypatch):
    tool, args, call_id, log_id = await pending_start(preview_case)
    armed, writes = False, []

    async def timeout(confirmation_id, **_kwargs):
        nonlocal armed
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                update(AiPreparedAction)
                .where(AiPreparedAction.confirmation_id == confirmation_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        armed = True
        raise TimeoutError()

    def capture(_conn, _cursor, sql, _params, context, _many):
        if armed and sql.startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(
                context.execution_options.get("_authorization_plan_permit") is not None
            )

    monkeypatch.setattr(executor.hitl_manager, "hang", timeout)
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        result = await executor._hang_for_confirmation(
            preview_case.deps, tool, log_id, call_id, args, "tool=user.export", None
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert result is None
    assert writes and all(writes), writes
    async with AsyncSessionLocal() as db:
        action = await db.scalar(
            select(AiPreparedAction).where(
                AiPreparedAction.execute_tool_call_id == call_id
            )
        )
        log = await db.get(AiOperationLog, log_id)
    assert action.status == log.status == "expired"
    assert action.error_code == log.error_code == "AI_HITL_EXPIRED"


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("committed", [False, True])
async def test_gateway_timeout_never_guesses_expiration(
    preview_case, monkeypatch, expired, committed
):
    tool, args, call_id, log_id = await pending_start(preview_case)
    attempts = []
    commit = authorization_lifecycle_owner.commit_authorization_session

    async def fault(db):
        attempts.append(True)
        if committed:
            await commit(db)
        raise BusinessException(
            503,
            "injected expiry commit ACK loss",
            error_code="AUTHORIZATION_COMMIT_UNKNOWN",
        )

    async def timeout(confirmation_id, **_kwargs):
        if expired:
            async with AsyncSessionLocal() as db, db.begin():
                await db.execute(
                    update(AiPreparedAction)
                    .where(AiPreparedAction.confirmation_id == confirmation_id)
                    .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
        raise TimeoutError()

    monkeypatch.setattr(executor.hitl_manager, "hang", timeout)
    monkeypatch.setattr(
        authorization_lifecycle_owner, "commit_authorization_session", fault
    )
    with pytest.raises(PreparedExecutionOutcomeUnknown):
        await executor._hang_for_confirmation(
            preview_case.deps, tool, log_id, call_id, args, "tool=user.export", None
        )
    assert len(attempts) == int(expired)
    async with AsyncSessionLocal() as db:
        action = await db.scalar(
            select(AiPreparedAction).where(
                AiPreparedAction.execute_tool_call_id == call_id
            )
        )
        log = await db.get(AiOperationLog, log_id)
    assert (
        action.status
        == log.status
        == ("expired" if expired and committed else "pending_confirmation")
    )
    assert preview_case.calls == []


@pytest.mark.parametrize("committed", [False, True])
async def test_pending_unknown_ack_preserves_original_identity(
    preview_case, monkeypatch, committed
):
    tool, args, call_id, log_id = await pending_start(preview_case)
    async with AsyncSessionLocal() as db:
        original_id = (await db.get(AiOperationLog, log_id)).confirmation_id
    commit = authorization_pending_owner.commit_authorization_session
    attempts = []

    async def fault(db):
        attempts.append(True)
        if committed:
            await commit(db)
        raise BusinessException(
            503, "injected commit ACK loss", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )

    monkeypatch.setattr(
        authorization_pending_owner, "commit_authorization_session", fault
    )
    with pytest.raises(PreparedExecutionOutcomeUnknown):
        await executor._hang_for_confirmation(
            preview_case.deps, tool, log_id, call_id, args, "tool=user.export", None
        )
    async with AsyncSessionLocal() as db:
        action = await db.scalar(
            select(AiPreparedAction).where(
                AiPreparedAction.confirmation_id == original_id
            )
        )
        log = await db.get(AiOperationLog, log_id)
    assert attempts == [True]
    assert (action is not None) == committed
    assert log.confirmation_id == original_id and log.status == "pending_confirmation"
    if action:
        assert (
            action.execute_tool_call_id == call_id
            and action.status == "pending_confirmation"
        )
    assert preview_case.calls == []


@pytest.mark.parametrize("fault", ["version", "scope", "source"])
async def test_pending_rechecks_before_persisting(preview_case, monkeypatch, fault):
    tool, args, call_id, log_id = await pending_start(preview_case)
    prepare = authorization_pending_owner.PendingOwner.prepare
    version = None

    async def revoke(self, **request):
        nonlocal version
        if fault == "scope":
            request["scope_hash"] = "0" * 64
        else:
            async with AsyncSessionLocal() as db, db.begin():
                if fault == "version":
                    actor = await db.get(User, preview_case.case.tenant.actor_user_id)
                    version = actor.auth_version
                    actor.auth_version += 1
                else:
                    await db.execute(
                        update(AiMessage)
                        .where(
                            AiMessage.message_id
                            == preview_case.deps.source_user_message_id
                        )
                        .values(is_active=False)
                    )
        return await prepare(self, **request)

    monkeypatch.setattr(authorization_pending_owner.PendingOwner, "prepare", revoke)
    try:
        with pytest.raises(BusinessException):
            await executor._hang_for_confirmation(
                preview_case.deps, tool, log_id, call_id, args, "tool=user.export", None
            )
        async with AsyncSessionLocal() as db:
            assert (
                await db.scalar(
                    select(AiPreparedAction.action_id).where(
                        AiPreparedAction.execute_tool_call_id == call_id
                    )
                )
                is None
            )
        assert preview_case.calls == []
    finally:
        if version is not None:
            async with AsyncSessionLocal() as db, db.begin():
                await db.execute(
                    update(User)
                    .where(User.user_id == preview_case.case.tenant.actor_user_id)
                    .values(auth_version=version)
                )


@pytest.mark.parametrize("prepared", [False, True])
async def test_actual_pending_write_plan_and_redis_after_transaction(
    preview_case, monkeypatch, prepared
):
    deps = preview_case.deps
    registered = ToolRegistry.get().find("user.export")
    args = {"reason": "fixed pending owner"}
    call_id = "tc_" + uuid4().hex
    writes, redis_transactions, active = [], [], set()

    def begin(connection):
        active.add(id(connection))

    def end(connection):
        active.discard(id(connection))

    def capture(_connection, _cursor, sql, _params, context, _many):
        statement = context.compiled.statement if context.compiled else None
        table = getattr(getattr(statement, "table", None), "name", "")
        if (
            sql.startswith("INSERT")
            and table in {"ai_prepared_action", "ai_operation_log"}
        ) or (sql.startswith("UPDATE") and table == "ai_operation_log"):
            writes.append(
                (
                    table,
                    context.execution_options.get("_authorization_plan_permit")
                    is not None,
                )
            )

    original = executor.hitl_manager.bind_durable_action

    async def bind(*args, **kwargs):
        redis_transactions.append(len(active))
        return await original(*args, **kwargs)

    async def stop(event):
        if isinstance(event, ConfirmationRequiredEvent):
            raise RuntimeError("stop after committed pending receipt")

    deps.signal_event = stop
    monkeypatch.setattr(executor.hitl_manager, "bind_durable_action", bind)
    for name, callback in (
        ("begin", begin),
        ("commit", end),
        ("rollback", end),
        ("before_cursor_execute", capture),
    ):
        event.listen(engine.sync_engine, name, callback)
    try:
        if prepared:
            preview_case.args["requested_outcome"] = "execute_if_approved"
            with pytest.raises(PreparedExecutionOutcomeUnknown):
                await executor.execute_tool(
                    "user.import_preview", preview_case.args, deps
                )
        else:
            log_id = await executor._start_log(
                deps,
                registered,
                call_id,
                compute_args_hash(args),
                "tool=user.export",
                AiExecutionMode.HITL,
            )
            with pytest.raises(PreparedExecutionOutcomeUnknown) as failure:
                await executor._hang_for_confirmation(
                    deps, registered, log_id, call_id, args, "tool=user.export", None
                )
            assert isinstance(failure.value.__cause__, RuntimeError)
    finally:
        for name, callback in (
            ("begin", begin),
            ("commit", end),
            ("rollback", end),
            ("before_cursor_execute", capture),
        ):
            event.remove(engine.sync_engine, name, callback)
    assert {table for table, _ in writes} == {"ai_prepared_action", "ai_operation_log"}
    assert all(planned for _, planned in writes), writes
    assert redis_transactions == [0]
