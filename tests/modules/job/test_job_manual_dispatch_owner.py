"""Signed notices are consumed once by a new transaction and fresh authority."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.core.exceptions import BusinessException
from app.db import authorization_job_owner as owner_module
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.job import job_runner
from app.modules.job.dispatch import verify_dispatch
from app.modules.job.models.job import SysJob, SysJobLog
from app.modules.system.models.user import User
from app.modules.system.schemas.user import UpdateProfile
from tests.modules.job.test_job_control_writes import definition
from tests.modules.system.test_authorization_system_candidate import (
    TENANT,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.fixture
async def signed_job(system_sessions):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    created = await runtime.job.execute(
        tenant=TENANT, actor_user_version=1, operation="create", payload=definition()
    )
    notice = await runtime.job.execute(
        tenant=TENANT,
        actor_user_version=1,
        operation="run",
        ids=(int(created["job_id"]),),
    )
    return runtime, plain, int(created["job_id"]), notice["dispatch"]


async def test_simultaneous_duplicate_notices_reserve_one_nonce_and_execution(
    signed_job,
):
    runtime, plain, identifier, token = signed_job
    claims = await asyncio.gather(
        *(runtime.job.claim(dispatch=token, runner_id="runner") for _ in range(2))
    )
    (claim,) = [value for value in claims if value is not None]
    assert claim.job["job_id"] == identifier
    await runtime.job.finish(claim, status="1")
    assert await runtime.job.claim(dispatch=token, runner_id="runner") is None
    async with plain() as db:
        logs = list(await db.scalars(select(SysJobLog)))
        assert (
            len(logs) == 1 and logs[0].dispatch_id == verify_dispatch(token).dispatch_id
        )
        assert logs[0].dispatch_expires_at.tzinfo is not None


@pytest.mark.parametrize("change", ["epoch", "identity"])
async def test_actor_or_epoch_changes_between_notification_and_claim_fail_closed(
    signed_job, change
):
    runtime, plain, _, token = signed_job
    if change == "epoch":
        await runtime.system(
            tenant=TENANT,
            actor_user_version=1,
            operation="user.profile",
            payload=UpdateProfile(nickname="after issue"),
        )
    else:
        async with plain() as db:
            await db.execute(update(User).where(User.user_id == 101).values(status="2"))
            await db.commit()
    with pytest.raises(BusinessException):
        await runtime.job.claim(dispatch=token, runner_id="runner")
    async with plain() as db:
        assert list(await db.scalars(select(SysJobLog.job_log_id))) == []


@pytest.mark.parametrize("after_commit", [False, True])
async def test_unknown_claim_ack_never_enters_task_code(
    signed_job, monkeypatch, after_commit
):
    runtime, plain, _, token = signed_job
    original = owner_module.commit_authorization_session

    async def unknown(db):
        if after_commit:
            await original(db)
        raise BusinessException(
            503, "Unknown acknowledgement", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )

    task = AsyncMock(return_value=("1", None, 1))
    monkeypatch.setattr(owner_module, "commit_authorization_session", unknown)
    monkeypatch.setattr(job_runner, "get_authorization_runtime", lambda: runtime)
    monkeypatch.setattr(job_runner, "_run_task", task)
    await job_runner.run_job_manual(token)
    task.assert_not_awaited()
    async with plain() as db:
        logs = list(await db.scalars(select(SysJobLog)))
        assert len(logs) == int(after_commit) and all(log.status == "3" for log in logs)


async def test_changed_config_invalidates_original_notice(signed_job):
    runtime, plain, identifier, token = signed_job
    async with plain() as db:
        await db.execute(
            update(SysJob).where(SysJob.job_id == identifier).values(timeout_seconds=10)
        )
        await db.commit()
    with pytest.raises(BusinessException) as denied:
        await runtime.job.claim(dispatch=token, runner_id="runner")
    assert denied.value.error_code == "JOB_DISPATCH_STALE"


async def test_replay_protection_survives_cleanup_and_parent_delete(signed_job):
    runtime, plain, identifier, token = signed_job
    claim = await runtime.job.claim(dispatch=token, runner_id="runner")
    await runtime.job.finish(claim, status="1")
    async with plain() as db:
        await db.execute(
            update(SysJobLog).values(start_time=datetime.now() - timedelta(days=5))
        )
        await db.commit()
    count = await runtime.job.execute(
        tenant=TENANT, actor_user_version=1, operation="log-clean", days=1
    )
    assert count == 0
    with pytest.raises(BusinessException) as denied:
        await runtime.job.execute(
            tenant=TENANT, actor_user_version=1, operation="delete", ids=(identifier,)
        )
    assert denied.value.error_code == "JOB_DISPATCH_PROTECTED"
    assert await runtime.job.claim(dispatch=token, runner_id="runner") is None
