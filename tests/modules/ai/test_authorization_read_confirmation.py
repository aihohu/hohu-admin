"""Forced HITL for real read tools uses fixed authorization owners."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import anyio
import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.exceptions import BusinessException
from app.db import authorization_read_confirmation_owner as owner_module
from app.db.authorization_http import get_authorization_runtime
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.session import engine
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.gateway.atomic_execution import (
    PreparedExecutionOutcomeUnknown,
)
from app.modules.ai.agents.hitl.events import ConfirmationRequiredEvent
from app.modules.ai.api import confirm as confirm_module
from app.modules.ai.api import resume as resume_module
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.confirm import ConfirmRequest
from app.modules.ai.service import (
    authorization_gateway_read_candidate as read_module,
)
from app.modules.ai.service.authorization_lifecycle_candidate import lifecycle_candidate
from app.modules.auth.service import access_identity
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_preview_atomic_completion import authenticated_actor
from tests.modules.ai.test_preview_atomic_completion import preview_case as preview_case


@pytest.mark.parametrize("tool", ["user.count", "user.list"])
async def test_read_confirmation_uses_only_planned_factual_writes(preview_case, tool):
    deps = preview_case.deps
    deps.injection_hit = True
    armed, writes = False, []
    async with AsyncSessionLocal() as db:
        epoch = await db.scalar(
            select(UserFieldPolicyState.epoch).where(
                UserFieldPolicyState.tenant_id == 0
            )
        )

    def capture(_conn, _cursor, sql, _params, context, _many):
        if armed and sql.startswith(("INSERT", "UPDATE", "DELETE")):
            statement = context.compiled.statement if context.compiled else None
            writes.append(
                (
                    getattr(getattr(statement, "table", None), "name", ""),
                    context.execution_options.get("_authorization_plan_permit")
                    is not None,
                )
            )

    async def approve(event):
        nonlocal armed
        if not isinstance(event, ConfirmationRequiredEvent):
            return
        armed = True
        try:
            async with AsyncSessionLocal() as db:
                actor = await authenticated_actor(db, tenant=deps.tenant)
                response = await confirm_module.confirm_tool(
                    ConfirmRequest(
                        confirmation_id=event.confirmation_id, action="approve"
                    ),
                    db=db,
                    current_user=actor,
                )
            assert response.data.status == "succeeded"
        finally:
            armed = False
        raise RuntimeError("stop after confirmed readonly result")

    deps.signal_event = approve
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(PreparedExecutionOutcomeUnknown):
            await executor.execute_tool(tool, {}, deps)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert writes and all(planned for _, planned in writes), writes
    assert {table for table, _ in writes} <= {
        "ai_prepared_action",
        "ai_operation_log",
        "ai_message",
    }
    async with AsyncSessionLocal() as db:
        action = await db.scalar(
            select(AiPreparedAction).where(
                AiPreparedAction.conversation_id == deps.conversation_id,
                AiPreparedAction.execute_tool_name == tool,
            )
        )
        assert action.status == "succeeded" and action.result_data is not None
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == epoch
        )


async def pending_read(preview_case):
    deps = preview_case.deps
    deps.injection_hit = True

    async def stop(event):
        if isinstance(event, ConfirmationRequiredEvent):
            raise RuntimeError("leave durable pending confirmation")

    deps.signal_event = stop
    with pytest.raises(PreparedExecutionOutcomeUnknown):
        await executor.execute_tool("user.count", {}, deps)
    async with AsyncSessionLocal() as db:
        return await db.scalar(
            select(AiPreparedAction).where(
                AiPreparedAction.conversation_id == deps.conversation_id,
                AiPreparedAction.execute_tool_name == "user.count",
            )
        )


@pytest.mark.parametrize("committed", [False, True])
async def test_read_confirmation_commit_uncertainty_never_claims_success(
    preview_case, monkeypatch, committed
):

    action = await pending_read(preview_case)
    calls, commits = [], []
    handler = read_module.READ_HANDLERS["user.count"]
    commit = owner_module.commit_authorization_session

    async def counted(*args, **kwargs):
        calls.append(True)
        return await handler(*args, **kwargs)

    async def fault(db):
        commits.append(True)
        if committed:
            await commit(db)
        raise BusinessException(
            503,
            "injected commit uncertainty",
            error_code="AUTHORIZATION_COMMIT_UNKNOWN",
        )

    monkeypatch.setattr(
        read_module,
        "READ_HANDLERS",
        {**read_module.READ_HANDLERS, "user.count": counted},
    )
    monkeypatch.setattr(owner_module, "commit_authorization_session", fault)
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(db, tenant=preview_case.deps.tenant)
        response = await confirm_module.confirm_tool(
            ConfirmRequest(confirmation_id=action.confirmation_id, action="approve"),
            db=db,
            current_user=actor,
        )
    assert response.data.status == "running"
    assert len(commits) == len(calls) == 1
    async with AsyncSessionLocal() as db:
        stored = await db.get(AiPreparedAction, action.action_id)
        assert stored.status == ("succeeded" if committed else "pending_confirmation")
        assert (stored.result_data is not None) == committed
    if committed:
        monkeypatch.setattr(owner_module, "commit_authorization_session", commit)
        async with AsyncSessionLocal() as db:
            actor = await authenticated_actor(db, tenant=preview_case.deps.tenant)
            response = await confirm_module.confirm_tool(
                ConfirmRequest(
                    confirmation_id=action.confirmation_id, action="approve"
                ),
                db=db,
                current_user=actor,
            )
        assert response.data.status == "succeeded"
        assert len(calls) == 1


async def test_read_result_uses_fixed_projection_and_rejects_tampered_fact(
    preview_case, monkeypatch
):

    action = await pending_read(preview_case)
    deps = preview_case.deps
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(db, tenant=deps.tenant)
        identity = access_identity(actor)
        response = await confirm_module.confirm_tool(
            ConfirmRequest(confirmation_id=action.confirmation_id, action="approve"),
            db=db,
            current_user=actor,
        )
    assert response.data.status == "succeeded"
    binding = executor._pending_binding(deps, action)

    async def forbidden(*_args, **_kwargs):
        pytest.fail("legacy terminal reader called")

    monkeypatch.setattr(executor, "_get_prepared_action_by_confirmation", forbidden)
    result, _duration = await executor._load_prepared_terminal_result(
        action.confirmation_id, tenant=deps.tenant, deps=deps, binding=binding
    )
    assert result.ok and result.data is not None and result.projection.scope_bound
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            update(AiPreparedAction)
            .where(AiPreparedAction.action_id == action.action_id)
            .values(result_data={"forged": True})
        )
    with pytest.raises(BusinessException):
        await get_authorization_runtime().lifecycle.resume_read(
            action.confirmation_id,
            tenant=identity.tenant,
            actor_user_version=identity.user_version,
            resume_binding=binding,
        )
    with pytest.raises(PreparedExecutionOutcomeUnknown):
        await executor._load_prepared_terminal_result(
            action.confirmation_id, tenant=deps.tenant, deps=deps, binding=binding
        )


@pytest.mark.parametrize("decision", ["reject", "timeout", "stale"])
async def test_read_cancellation_never_queries_business_data(
    preview_case, monkeypatch, decision
):

    action = await pending_read(preview_case)
    deps = preview_case.deps
    async with AsyncSessionLocal() as db, db.begin():
        if decision == "timeout":
            await db.execute(
                update(AiPreparedAction)
                .where(AiPreparedAction.action_id == action.action_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        elif decision == "stale":
            await db.execute(
                update(AiMessage)
                .where(AiMessage.message_id == action.source_user_message_id)
                .values(is_active=False)
            )

    async def forbidden(*_args, **_kwargs):
        pytest.fail("cancelled read invoked business query")

    monkeypatch.setattr(
        read_module,
        "READ_HANDLERS",
        {**read_module.READ_HANDLERS, "user.count": forbidden},
    )
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(db, tenant=deps.tenant)
        identity = access_identity(actor)
        db.expunge_all()
    result = await get_authorization_runtime().read_confirmation.confirm(
        action.confirmation_id,
        tenant=identity.tenant,
        actor_user_version=identity.user_version,
        decision="approve" if decision == "stale" else decision,
    )
    assert result.status == ("rejected" if decision == "reject" else "expired")


async def test_concurrent_read_confirmations_execute_once(preview_case, monkeypatch):
    action = await pending_read(preview_case)
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(db, tenant=preview_case.deps.tenant)
        identity = access_identity(actor)
    calls = []
    handler = read_module.READ_HANDLERS["user.count"]

    async def counted(*args, **kwargs):
        calls.append(True)
        return await handler(*args, **kwargs)

    monkeypatch.setattr(
        read_module,
        "READ_HANDLERS",
        {**read_module.READ_HANDLERS, "user.count": counted},
    )

    async def confirm():
        return await get_authorization_runtime().read_confirmation.confirm(
            action.confirmation_id,
            tenant=identity.tenant,
            actor_user_version=identity.user_version,
            decision="approve",
        )

    results = await asyncio.gather(confirm(), confirm(), return_exceptions=True)
    assert len(calls) == 1
    assert any(
        not isinstance(result, BaseException) and result.status == "succeeded"
        for result in results
    )
    for result in results:
        if isinstance(result, BaseException):
            assert isinstance(result, BusinessException)
            assert result.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
        else:
            assert result.status == "succeeded"


async def test_read_resume_poll_and_expiry_use_fixed_owners(preview_case, monkeypatch):
    action = await pending_read(preview_case)
    deps = preview_case.deps
    pending = SimpleNamespace(
        action_id=action.action_id,
        conversation_id=action.conversation_id,
        source_user_message_id=action.source_user_message_id,
        trace_id=action.trace_id,
        tool_call_id=action.execute_tool_call_id,
        tool_name=action.execute_tool_name,
        args=action.frozen_args,
        user_id=action.user_id,
        tenant_id=action.tenant_id,
        agent_code=action.agent_code,
    )
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(db, tenant=deps.tenant)
        db.expunge_all()

    def forbidden(*_args, **_kwargs):
        pytest.fail("legacy polling session called")

    monkeypatch.setattr(resume_module, "AsyncSessionLocal", forbidden)
    with pytest.raises(TimeoutError):
        await resume_module._wait_durable_resume_terminal(
            confirmation_id=action.confirmation_id,
            pending=pending,
            user_id=action.user_id,
            tenant=deps.tenant,
            timeout_sec=0,
            current_user=actor,
        )
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            update(AiPreparedAction)
            .where(AiPreparedAction.action_id == action.action_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    events = await resume_module._terminalize_durable_resume_failure(
        confirmation_id=action.confirmation_id,
        pending=pending,
        user_id=action.user_id,
        current_user=actor,
        tenant=deps.tenant,
        error_code="AI_HITL_TIMEOUT",
        error_msg="timeout",
    )
    assert events[0].error_code == "AI_HITL_EXPIRED"
    await resume_module._wait_durable_resume_terminal(
        confirmation_id=action.confirmation_id,
        pending=pending,
        user_id=action.user_id,
        tenant=deps.tenant,
        timeout_sec=0,
        current_user=actor,
    )


async def test_planned_poll_disconnect_returns_postgres_connection(
    preview_case, monkeypatch
):
    action = await pending_read(preview_case)
    deps = preview_case.deps
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(db, tenant=deps.tenant)
        identity = access_identity(actor)
    isolated = create_async_engine(engine.url, pool_size=1, max_overflow=0)
    runtime = AuthorizationRuntime(isolated)
    original = lifecycle_candidate._locked
    completed = []
    try:
        for _ in range(3):
            with anyio.CancelScope() as scope:

                async def interrupted(*args, **kwargs):
                    result = await original(*args, **kwargs)
                    completed.append(True)
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()
                    return result

                monkeypatch.setattr(lifecycle_candidate, "_locked", interrupted)
                with pytest.raises(asyncio.CancelledError):
                    await runtime.lifecycle.poll(
                        action.confirmation_id,
                        tenant=identity.tenant,
                        actor_user_version=identity.user_version,
                        resume_binding=executor._pending_binding(deps, action),
                    )
            assert isolated.pool.checkedout() == 0
        assert len(completed) == 3
    finally:
        await isolated.dispose()
