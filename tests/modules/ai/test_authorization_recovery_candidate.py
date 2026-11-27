"""Protected recovery preserves unknown and never reauthorizes a handler."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, update

from app.core.authorization_plan import LockMode, LockPlan, TenantLock
from app.core.tenant import TenantContext
from app.db.authorization_ai_owner import AIAuthorizationOwner
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.completion_read_state_service import (
    completion_read_state_service,
)
from app.modules.system.models.user import User
from app.modules.system.service.authorization_transaction_service import (
    _Transaction,
    authorization_transactions,
)
from tests.modules.ai.test_atomic_completion_service import atomic_case
from tests.modules.system.test_authorization_system_candidate import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)


@pytest.fixture
async def recovery_case(system_sessions):
    factory, plain = system_sessions
    async with plain() as db:
        await db.execute(
            update(User).where(User.user_id == 101).values(user_name="admin")
        )
        case = await atomic_case.__wrapped__(db)
        await db.commit()
    case.tenant = TenantContext(0, "plan-0", 101, 1, "access_token")
    return factory, plain, case


async def recover(factory, case, **kwargs):

    return await AIAuthorizationOwner(factory).reconcile(
        case.action.confirmation_id, tenant=case.tenant, **kwargs
    )


async def test_active_lease_is_unknown_and_never_fenced(recovery_case):
    factory, plain, case = recovery_case
    assert (await recover(factory, case)).disposition == "unknown"
    async with plain() as db:
        assert await db.scalar(select(AiPreparedAction.status)) == "running"


async def test_expired_db_claim_recovers_after_actor_revocation(recovery_case):
    factory, plain, case = recovery_case
    async with plain() as db:
        await db.execute(
            update(User).where(User.user_id == 101).values(status="2", auth_version=9)
        )
        await db.execute(
            update(AiPreparedAction).values(
                execution_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db.commit()
    outcome = await recover(factory, case)
    assert outcome.disposition == "failed"
    async with plain() as db:
        action = await db.scalar(select(AiPreparedAction))
        log = await db.scalar(select(AiOperationLog))
        assert action.status == log.status == "failed"
        assert action.row_version == case.claim.row_version + 1
        assert action.execution_owner == case.claim.execution_owner
        assert (
            action.error_code
            == log.error_code
            == "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
        )
    repeated = await recover(factory, case)
    assert repeated.disposition == "failed"
    assert repeated.action.status == "failed"
    assert repeated.action.action_id == case.claim.action_id


async def test_explicit_claim_mismatch_cannot_fence(recovery_case):
    factory, plain, case = recovery_case
    assert (
        await recover(
            factory,
            case,
            expected_claim=replace(case.claim, args_hash="a" * 64),
            allow_fence=True,
        )
    ).disposition == "unknown"
    async with plain() as db:
        assert await db.scalar(select(AiPreparedAction.status)) == "running"


@pytest.mark.parametrize(
    "owner,tool",
    [
        ("legacy", "user.create"),
        ("v0162-db:invalid", "user.create"),
        ("v0162-file:invalid", "user.export"),
    ],
)
async def test_legacy_and_unproven_file_remain_unknown(recovery_case, owner, tool):
    factory, plain, case = recovery_case
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                execution_owner=owner,
                execute_tool_name=tool,
                execution_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await db.commit()
    assert (await recover(factory, case)).disposition == "unknown"
    async with plain() as db:
        assert await db.scalar(select(AiPreparedAction.status)) == "running"


async def test_required_log_failure_rolls_back_action(recovery_case, monkeypatch):
    factory, plain, case = recovery_case
    original = _Transaction.update_factual

    async def fail_log(self, key, *args, **kwargs):
        if key.kind.value == "operation_log":
            raise RuntimeError("required log unavailable")
        return await original(self, key, *args, **kwargs)

    monkeypatch.setattr(_Transaction, "update_factual", fail_log)
    assert (
        await recover(factory, case, expected_claim=case.claim, allow_fence=True)
    ).disposition == "unknown"
    async with plain() as db:
        assert await db.scalar(select(AiPreparedAction.status)) == "running"
        assert await db.scalar(select(AiOperationLog.status)) == "running"


async def test_recovery_takes_guard_barrier_before_complete_context(recovery_case):
    factory, plain, case = recovery_case
    seen = []

    def record(_conn, _cursor, sql, _parameters, _context, _many):
        if "FOR UPDATE" in sql or "FOR SHARE" in sql:
            seen.append(sql)

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
    try:
        await recover(factory, case)
    finally:
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
    expected = [
        "sys_user_field_policy_global_guard",
        "sys_user_field_policy_state",
        "ai_conversation",
        "ai_message",
        "ai_prepared_action",
        "ai_operation_log",
    ]
    assert len(seen) == len(expected)
    assert all(table in sql for table, sql in zip(expected, seen, strict=True))


async def test_lost_commit_ack_keeps_unknown_and_later_reads_committed_fact(
    recovery_case, monkeypatch
):
    factory, plain, case = recovery_case
    first = True

    def uncertain_factory():
        nonlocal first
        db = factory()
        if first:
            first = False
            commit = db.commit

            async def lose_ack():
                await commit()
                raise ConnectionError("ACK lost after commit")

            monkeypatch.setattr(db, "commit", lose_ack)
        return db

    outcome = await AIAuthorizationOwner(uncertain_factory).reconcile(
        case.action.confirmation_id,
        tenant=case.tenant,
        expected_claim=case.claim,
        allow_fence=True,
    )
    assert outcome.disposition == "unknown"
    assert (await recover(factory, case)).disposition == "failed"
    async with plain() as db:
        assert (
            await db.scalar(select(AiPreparedAction.row_version))
            == case.claim.row_version + 1
        )


async def test_rr_fact_read_does_not_wait_for_outer_exclusive_barrier(
    recovery_case, monkeypatch
):

    factory, plain, case = recovery_case
    monkeypatch.setattr(
        "app.db.authorization_http.get_authorization_runtime",
        lambda: AuthorizationRuntime(plain.kw["bind"]),
    )
    async with factory() as db:
        async with authorization_transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            await tx.lock(LockPlan())
            async with asyncio.timeout(1):
                states = await completion_read_state_service.read(
                    tenant=case.tenant,
                    tool_call_ids=(case.action.execute_tool_call_id,),
                )
            assert states[case.action.execute_tool_call_id].unknown
