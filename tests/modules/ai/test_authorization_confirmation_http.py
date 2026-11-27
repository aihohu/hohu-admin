"""Actual pending confirmation ingress must enter G before context locks."""

# ruff: noqa: PLC0415

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.exceptions import setup_exception_handlers
from app.core.security import create_access_token
from app.db import session
from app.db.request_session import RequestSession
from app.db.session import engine
from app.modules.ai.api import confirm
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.auth import service
from app.modules.system.models.user import User
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_atomic_prepared_completion import count_real_handler


@pytest.mark.parametrize(
    "decision",
    ["approve", "reject", "expired", "model_revoked", "source_stale", "disabled"],
)
async def test_pending_confirmation_http_has_global_first(
    completion_case, monkeypatch, decision
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    async with AsyncSessionLocal() as db:
        version = await db.scalar(
            select(User.auth_version).where(User.user_id == case.tenant.actor_user_id)
        )
        if decision == "expired":
            await db.execute(
                update(AiPreparedAction)
                .where(AiPreparedAction.action_id == case.action_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await db.commit()
    monkeypatch.setattr(
        session,
        "AsyncSessionLocal",
        async_sessionmaker(engine, class_=RequestSession, expire_on_commit=False),
    )
    monkeypatch.setattr(service, "_is_blacklisted", AsyncMock(return_value=False))
    release_reads = confirm.release_request_reads
    entered = []

    async def after_auth(db):
        await release_reads(db)
        entered.append(True)
        assert not db.in_transaction()
        if decision in {"model_revoked", "source_stale"}:
            async with AsyncSessionLocal() as writer, writer.begin():
                action = await writer.get(AiPreparedAction, case.action_id)
                if decision == "model_revoked":
                    await writer.execute(
                        update(TenantAiModelPolicy)
                        .where(
                            TenantAiModelPolicy.tenant_id == case.tenant.tenant_id,
                            TenantAiModelPolicy.model_id == action.resolved_model_id,
                        )
                        .values(enabled=False)
                    )
                else:
                    source = await writer.get(AiMessage, action.source_user_message_id)
                    source.is_active = False

    monkeypatch.setattr(confirm, "release_request_reads", after_auth)
    if decision == "disabled":
        monkeypatch.setattr(
            confirm, "check_user_disabled", AsyncMock(return_value=True)
        )
    app = FastAPI()
    setup_exception_handlers(app)
    app.include_router(confirm.router, prefix="/ai/confirm")
    token = create_access_token(
        subject=str(case.tenant.actor_user_id),
        tenant_id=0,
        tenant_version=1,
        user_version=version,
    )
    locks = []

    def observe(_conn, _cursor, sql, _params, _context, _many):
        if "FOR UPDATE" in sql or "FOR SHARE" in sql:
            locks.append(sql)

    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/ai/confirm",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "confirmationId": case.confirmation_id,
                    "action": "reject" if decision == "reject" else "approve",
                },
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
    assert entered == [True]
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    assert len(case.calls) == int(decision == "approve")
    async with AsyncSessionLocal() as db:
        action = await db.get(AiPreparedAction, case.action_id)
        log = await db.get(AiOperationLog, case.log_id)
        if decision in {"model_revoked", "disabled"}:
            assert response.status_code == (
                400 if decision == "model_revoked" else 403
            ), response.text
            assert action.status == log.status == "expired"
            assert action.error_code == log.error_code
            assert log.started_at is None
            assert action.execution_owner is None
            if decision == "disabled":
                assert action.error_code == "AI_USER_DISABLED"
        else:
            assert response.status_code == 200, response.text
            expected = {
                "approve": "succeeded",
                "reject": "rejected",
                "expired": "expired",
                "source_stale": "expired",
            }[decision]
            assert response.json()["data"]["status"] == action.status == expected


@pytest.mark.parametrize("mode", ["drop_before", "drop_after"])
async def test_preclaim_denial_unknown_ack_never_executes(
    completion_case, monkeypatch, mode
):
    from app.core.exceptions import BusinessRuleException
    from app.modules.system.service.authorization_ai_tool_adapter import (
        system_ai_tool_adapter,
    )
    from tests.modules.ai.test_atomic_prepared_completion import observe
    from tests.modules.ai.test_real_commit_ack import approve_via, fault_sessions

    case = completion_case
    count_real_handler(case, monkeypatch)
    stale = BusinessRuleException(
        "stale", error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE"
    )
    monkeypatch.setattr(
        system_ai_tool_adapter, "validate_planned", AsyncMock(side_effect=stale)
    )
    notify = AsyncMock()
    monkeypatch.setattr(confirm, "_notify_prepared_terminal", notify)
    async with fault_sessions(case, monkeypatch, [("expired", mode)]) as (
        _,
        factory,
        faults,
    ):
        response = await approve_via(case, factory)
        assert faults[0].reached.is_set()
        assert response.data.status == "running"
        state = await observe(case)
        assert state["action_status"] == (
            "expired" if mode == "drop_after" else "pending_confirmation"
        )
        assert case.calls == [] and state["user_ids"] == []
        notify.assert_not_awaited()
        if mode == "drop_after":
            assert (await approve_via(case, factory)).data.status == "expired"
            assert case.calls == []


async def test_late_preclaim_denial_cannot_overwrite_another_success(
    completion_case, monkeypatch
):
    import asyncio

    from app.core.exceptions import BusinessRuleException
    from app.db.authorization_lifecycle_owner import LifecycleOwner
    from app.modules.system.service.authorization_ai_tool_adapter import (
        system_ai_tool_adapter,
    )
    from tests.modules.ai.test_atomic_prepared_completion import (
        approve,
        assert_success,
        observe,
    )

    case = completion_case
    count_real_handler(case, monkeypatch)
    real_validate = system_ai_tool_adapter.validate_planned
    real_admit = LifecycleOwner.admit
    rejected, release = asyncio.Event(), asyncio.Event()
    failures = []

    async def fail_once(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise BusinessRuleException(
                "stale", error_code="AI_PREPARED_ACTION_SNAPSHOT_STALE"
            )
        return await real_validate(*args, **kwargs)

    async def paused_admit(self, *args, **kwargs):
        if kwargs.get("denial") is not None:
            rejected.set()
            await release.wait()
        return await real_admit(self, *args, **kwargs)

    monkeypatch.setattr(system_ai_tool_adapter, "validate_planned", fail_once)
    monkeypatch.setattr(LifecycleOwner, "admit", paused_admit)
    first = asyncio.create_task(approve(case))
    try:
        await asyncio.wait_for(rejected.wait(), 5)
        assert (await asyncio.wait_for(approve(case), 10)).data.status == "succeeded"
        release.set()
        assert (await asyncio.wait_for(first, 5)).data.status == "running"
        assert_success(case, await observe(case))
        assert len(case.calls) == 1
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("reference_delay", [0, 2])
async def test_confirmation_input_preparation_has_total_deadline(
    completion_case, monkeypatch, reference_delay
):
    import asyncio

    from app.db.authorization_lifecycle_owner import LifecycleOwner
    from app.modules.ai.service.authorization_lifecycle_candidate import (
        lifecycle_candidate,
    )
    from app.modules.system.service.authorization_ai_tool_adapter import (
        system_ai_tool_adapter,
    )
    from tests.modules.ai.test_atomic_prepared_completion import approve, observe

    entered = asyncio.Event()
    cancelled = asyncio.Event()
    started = None
    original_confirm = LifecycleOwner.confirm
    original_reference = lifecycle_candidate.reference

    async def observed_confirm(self, *args, **kwargs):
        nonlocal started
        started = asyncio.get_running_loop().time()
        return await original_confirm(self, *args, **kwargs)

    async def delayed_reference(*args, **kwargs):
        if started is not None:
            await asyncio.sleep(reference_delay)
        return await original_reference(*args, **kwargs)

    async def stalled(db, **_kwargs):
        assert not db.in_transaction()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(LifecycleOwner, "confirm", observed_confirm)
    monkeypatch.setattr(lifecycle_candidate, "reference", delayed_reference)
    monkeypatch.setattr(system_ai_tool_adapter, "prepare_input", stalled)
    task = asyncio.create_task(approve(completion_case))
    try:
        # Authentication/admission precede the actual preparation budget.
        await asyncio.wait_for(entered.wait(), 20)
        assert started is not None
        remaining = max(0, 5.8 - (asyncio.get_running_loop().time() - started))
        await asyncio.wait_for(cancelled.wait(), remaining)
        # Rollback/close retain their independent cleanup budgets.
        done, _ = await asyncio.wait({task}, timeout=10)
        assert task in done, "Preparation cleanup did not finish"
        with pytest.raises(TimeoutError):
            task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    state = await observe(completion_case)
    assert state["action_status"] == "pending_confirmation"
    assert completion_case.calls == [] and state["user_ids"] == []


@pytest.mark.parametrize("overrun_stage", ["reference", "prepare"])
async def test_confirmation_sync_overrun_cannot_start_next_phase(
    completion_case, monkeypatch, overrun_stage
):
    import time

    from app.db.authorization_lifecycle_owner import LifecycleOwner
    from app.modules.ai.service.authorization_lifecycle_candidate import (
        lifecycle_candidate,
    )
    from app.modules.system.service.authorization_ai_tool_adapter import (
        system_ai_tool_adapter,
    )
    from tests.modules.ai.test_atomic_prepared_completion import approve, observe

    count_real_handler(completion_case, monkeypatch)
    original_confirm = LifecycleOwner.confirm
    original_reference = lifecycle_candidate.reference
    original_prepare = system_ai_tool_adapter.prepare_input
    in_confirmation = False
    prepare_calls = []

    async def observed_confirm(self, *args, **kwargs):
        nonlocal in_confirmation
        in_confirmation = True
        return await original_confirm(self, *args, **kwargs)

    async def late_reference(*args, **kwargs):
        value = await original_reference(*args, **kwargs)
        if in_confirmation and overrun_stage == "reference":
            # A synchronous tail cannot be preempted by an asyncio timer.
            time.sleep(5.05)  # noqa: ASYNC251 - Exercise an unpreemptible tail.
        return value

    async def late_prepare(*args, **kwargs):
        prepare_calls.append(True)
        value = await original_prepare(*args, **kwargs)
        if overrun_stage == "prepare":
            time.sleep(5.05)  # noqa: ASYNC251 - Exercise an unpreemptible tail.
        return value

    monkeypatch.setattr(LifecycleOwner, "confirm", observed_confirm)
    monkeypatch.setattr(lifecycle_candidate, "reference", late_reference)
    monkeypatch.setattr(system_ai_tool_adapter, "prepare_input", late_prepare)
    with pytest.raises(TimeoutError):
        await approve(completion_case)
    assert prepare_calls == ([] if overrun_stage == "reference" else [True])
    state = await observe(completion_case)
    assert state["action_status"] == "pending_confirmation"
    assert completion_case.calls == [] and state["user_ids"] == []


@pytest.mark.parametrize("terminal", [False, True])
async def test_confirmation_rollback_time_only_consumes_pending_preparation(
    completion_case, monkeypatch, terminal
):
    import asyncio
    from types import SimpleNamespace

    from app.db import authorization_lifecycle_owner as owner_module
    from app.db.authorization_runtime import AuthorizationRuntime
    from tests.modules.ai.test_atomic_prepared_completion import approve, observe

    count_real_handler(completion_case, monkeypatch)
    if terminal:
        assert (await approve(completion_case)).data.status == "succeeded"
    loop = asyncio.get_running_loop()
    elapsed = 0
    original_rollback = owner_module.rollback_authorization_session

    async def acknowledged_rollback(db):
        nonlocal elapsed
        await original_rollback(db)
        # Advance only this owner's clock after a real acknowledged rollback;
        # the runtime's actual transaction/ACK timers are not replaced.
        elapsed = 6

    monkeypatch.setattr(
        owner_module,
        "asyncio",
        SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(
                time=lambda: loop.time() + elapsed
            ),
            timeout_at=asyncio.timeout_at,
        ),
    )
    monkeypatch.setattr(
        owner_module, "rollback_authorization_session", acknowledged_rollback
    )
    owner = AuthorizationRuntime(engine).lifecycle
    request = {
        "confirmation_id": completion_case.confirmation_id,
        "tenant": completion_case.tenant,
        "actor_user_version": 1,
    }
    if terminal:
        assert (await owner.confirm(**request)).disposition == "succeeded"
    else:
        with pytest.raises(TimeoutError):
            await owner.confirm(**request)
    state = await observe(completion_case)
    assert state["action_status"] == (
        "succeeded" if terminal else "pending_confirmation"
    )
    assert len(completion_case.calls) == len(state["user_ids"]) == int(terminal)


async def test_confirmed_status_does_not_depend_on_redis_disable_lookup(
    completion_case, monkeypatch
):
    from tests.modules.ai.test_atomic_prepared_completion import approve

    count_real_handler(completion_case, monkeypatch)
    assert (await approve(completion_case)).data.status == "succeeded"
    disabled = AsyncMock(side_effect=TimeoutError("Redis unavailable"))
    monkeypatch.setattr(confirm, "check_user_disabled", disabled)
    assert (await approve(completion_case)).data.status == "succeeded"
    disabled.assert_not_awaited()
    assert len(completion_case.calls) == 1
