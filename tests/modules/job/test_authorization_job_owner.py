"""Independent job transactions cover concurrent claims and unknown commit ACKs."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.core.exceptions import BusinessException
from app.db import authorization_job_owner as owner_module
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.job import job_runner, task_registry
from app.modules.job.models.job import SysJob, SysJobLog
from app.modules.job.schemas.job import JobCreate
from app.modules.system.models.role import Role
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.fixture
async def jobs(system_sessions, monkeypatch):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    key = "planned_job_test"
    task = AsyncMock()
    monkeypatch.setitem(task_registry._task_functions, key, task)
    monkeypatch.setattr(job_runner, "get_authorization_runtime", lambda: runtime)
    await runtime.job.execute(
        tenant=TENANT,
        actor_user_version=1,
        operation="create",
        payload=JobCreate(
            job_key=key,
            job_name=key,
            cron_expression="0 * * * *",
            status="1",
            concurrent="2",
            max_retries=2,
        ),
    )
    async with plain() as db:
        identifier = await db.scalar(select(SysJob.job_id).where(SysJob.job_key == key))
    return runtime.job, plain, identifier, task


async def test_two_simultaneous_claims_reserve_one_execution(jobs):
    owner, plain, identifier, _ = jobs
    claims = await asyncio.gather(
        *(
            owner.claim(tenant_id=0, job_id=identifier, runner_id="runner")
            for _ in range(2)
        )
    )
    (claim,) = [value for value in claims if value is not None]
    assert await owner.finish(claim, status="1")
    assert not await owner.finish(claim, status="2", error_msg="late failure")
    async with plain() as db:
        rows = list(
            await db.scalars(select(SysJobLog).where(SysJobLog.job_id == identifier))
        )
        assert len(rows) == 1 and rows[0].status == "1"
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 1
        )


@pytest.mark.parametrize("after_commit", [False, True])
async def test_unknown_claim_ack_never_dispatches_task(jobs, monkeypatch, after_commit):
    _, plain, identifier, task = jobs
    original = owner_module.commit_authorization_session

    async def unknown(db):
        if after_commit:
            await original(db)
        raise BusinessException(
            503, "unknown", error_code="AI_EXECUTION_OUTCOME_UNKNOWN"
        )

    monkeypatch.setattr(owner_module, "commit_authorization_session", unknown)
    await job_runner._do_execute(0, identifier)
    task.assert_not_awaited()
    async with plain() as db:
        logs = list(
            await db.scalars(select(SysJobLog).where(SysJobLog.job_id == identifier))
        )
        assert len(logs) == int(after_commit)
        assert all(log.status == "3" for log in logs)


async def test_completion_ack_unknown_does_not_enter_task_retry_loop(jobs, monkeypatch):
    _, plain, identifier, task = jobs
    original, count = owner_module.commit_authorization_session, 0

    async def unknown(db):
        nonlocal count
        count += 1
        await original(db)
        if count == 2:
            raise BusinessException(
                503, "unknown", error_code="AI_EXECUTION_OUTCOME_UNKNOWN"
            )

    monkeypatch.setattr(owner_module, "commit_authorization_session", unknown)
    await job_runner._do_execute(0, identifier)
    task.assert_awaited_once()
    async with plain() as db:
        assert (
            await db.scalar(
                select(SysJobLog.status).where(SysJobLog.job_id == identifier)
            )
            == "1"
        )


async def test_revoke_before_command_leaves_job_and_logs_unchanged(jobs):
    owner, plain, identifier, _ = jobs
    async with plain() as db:
        await db.execute(
            update(Role).where(Role.role_id == 201).values(role_code="REVOKED")
        )
        await db.commit()
    with pytest.raises(BusinessException) as error:
        await owner.execute(
            tenant=TENANT,
            actor_user_version=1,
            operation="status",
            payload={"job_id": identifier, "status": "2"},
        )
    assert error.value.error_code == "MISSING_PERMISSION"
    async with plain() as db:
        assert (
            await db.scalar(select(SysJob.status).where(SysJob.job_id == identifier))
            == "1"
        )


async def test_delete_locks_and_explicitly_removes_log_children(jobs):
    owner, plain, identifier, _ = jobs
    claim = await owner.claim(tenant_id=0, job_id=identifier, runner_id="runner")
    await owner.finish(claim, status="1")
    await owner.execute(
        tenant=TENANT,
        actor_user_version=1,
        operation="status",
        payload={"job_id": identifier, "status": "2"},
    )
    assert (
        await owner.execute(
            tenant=TENANT, actor_user_version=1, operation="delete", ids=(identifier,)
        )
        == 1
    )
    async with plain() as db:
        assert not list(await db.scalars(select(SysJobLog)))
        assert await db.get(SysJob, identifier) is None
