"""Strict composition preserves atomic execution, lease and FILE ownership."""

import asyncio

import pytest
from sqlalchemy import select

from app.core.file_storage import LocalFileStorage
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.agent_admin import AgentAdminUpdateReq
from app.modules.system.models.dept import Dept
from app.modules.system.models.user_field_policy import UserFieldPolicyGlobalGuard
from tests.db.test_authorization_runtime_concurrency import CommitGate
from tests.modules.ai.test_authorization_gateway_execution import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_recovery_candidate import (
    recovery_case as recovery_case,
)
from tests.modules.system.test_authorization_system_candidate import (
    TENANT,
    epoch,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.mark.parametrize("lose_ack", [False, True])
async def test_strict_gateway_reconciles_original_claim_without_replay(
    execution_case, monkeypatch, lose_ack
):
    _, plain, case = execution_case
    runtime = AuthorizationRuntime(plain.kw["bind"])
    original = AuthorizationPlanSession.commit
    lost = False

    async def commit(db):
        nonlocal lost
        await original(db)
        if lose_ack and not lost:
            lost = True
            raise ConnectionError("injected post-commit ACK loss")

    monkeypatch.setattr(AuthorizationPlanSession, "commit", commit)
    first = await runtime.gateway.execute_claim(
        case.claim, tenant=case.tenant, actor_user_version=1
    )
    assert first.disposition == ("unknown" if lose_ack else "succeeded")
    # Reconcile the same claim; do not mint an execution owner or lease.
    recovered = await runtime.gateway.execute_claim(
        case.claim, tenant=case.tenant, actor_user_version=1
    )
    assert recovered.disposition == "succeeded"
    async with plain() as db:
        assert (await db.scalars(select(Dept.dept_name))).all() == ["Gateway team"]
        assert await db.scalar(select(AiPreparedAction.status)) == "succeeded"
        assert await db.scalar(select(AiOperationLog.status)) == "success"
        assert await epoch(db) == 2


async def test_strict_lease_exception_retains_exact_claim(execution_case):
    _, plain, case = execution_case
    runtime = AuthorizationRuntime(plain.kw["bind"])
    assert await runtime.lease.renew(case.claim) is True
    async with plain() as db:
        row = await db.scalar(select(AiPreparedAction))
        assert row.status == "running"
        assert row.row_version == case.claim.row_version
        assert row.execution_owner == case.claim.execution_owner
        assert await epoch(db) == 1


async def test_strict_file_owner_publishes_and_reads_exact_manifest(
    system_sessions, monkeypatch, tmp_path
):
    _, plain = system_sessions
    storage = LocalFileStorage(tmp_path)
    monkeypatch.setattr(
        "app.db.authorization_runtime.get_file_storage", lambda: storage
    )
    runtime = AuthorizationRuntime(plain.kw["bind"])
    result = await runtime.transfer.export(
        tenant=TENANT, actor_user_version=1, args={"reason": "audit"}
    )
    data, filename = await runtime.transfer.download(
        tenant=TENANT,
        actor_user_version=1,
        kind="export",
        identifier=result.resource_id,
    )
    assert data[:2] == b"PK" and filename.endswith(".xlsx")
    async with plain() as db:
        assert await epoch(db) == 1


@pytest.mark.parametrize("revoke_first", [False, True])
async def test_global_agent_revocation_and_ai_execution_serialize(
    execution_case, monkeypatch, revoke_first
):
    _, plain, case = execution_case
    runtime = AuthorizationRuntime(plain.kw["bind"])

    async def revoke():
        return await runtime.control(
            tenant=case.tenant,
            actor_user_version=1,
            operation="agent.update",
            target_id=8001,
            payload=AgentAdminUpdateReq(enabled=False),
            audit_context={
                "reason": "revoke",
                "ticket_id": "test",
                "correlation_id": "revoke-agent",
            },
        )

    async def execute():
        return await runtime.gateway.execute_claim(
            case.claim, tenant=case.tenant, actor_user_version=1
        )

    gate = CommitGate(
        monkeypatch,
        plain.kw["bind"],
        pause_sql="UPDATE ai_agent" if revoke_first else "INSERT INTO sys_dept",
    )
    tasks = []
    try:
        tasks.append(
            asyncio.create_task(
                revoke() if revoke_first else execute(), name="first-owner"
            )
        )
        await asyncio.wait_for(gate.entered.wait(), 1.5)
        tasks.append(
            asyncio.create_task(
                execute() if revoke_first else revoke(), name="second-owner"
            )
        )
        await gate.assert_blocked(plain)
        gate.release.set()
        results = await asyncio.gather(*tasks)
        ai_result = results[1] if revoke_first else results[0]
        assert ai_result.disposition == ("failed" if revoke_first else "succeeded")
        async with plain() as db:
            assert (
                await db.scalar(select(AiAgent.enabled).where(AiAgent.agent_id == 8001))
                is False
            )
            assert len((await db.scalars(select(Dept))).all()) == (
                0 if revoke_first else 1
            )
            assert await epoch(db) == (2 if revoke_first else 3)
            assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 2
    finally:
        gate.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
