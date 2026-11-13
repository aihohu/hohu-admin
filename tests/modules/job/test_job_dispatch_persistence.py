"""Nonce lineage survives cleanup until replay and unknown-result windows close."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.exceptions import BusinessException
from app.modules.job.models.job import SysJob, SysJobLog
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.fixture
async def nonce_sessions(system_sessions):
    factory, plain = system_sessions
    async with plain() as db:
        await db.execute(
            insert(SysJob).values(
                tenant_id=0,
                job_id=301,
                job_key="test_task",
                job_name="Test",
                status="2",
            )
        )
        await db.commit()
    return factory, plain


def log(**changes):
    return {
        "tenant_id": 0,
        "job_id": 301,
        "job_log_id": 401,
        "job_key": "test_task",
        "job_name": "Test",
        "status": "1",
        "start_time": datetime.now(),
        "dispatch_id": str(uuid4()),
        "dispatch_expires_at": datetime.now(UTC) + timedelta(seconds=30),
        **changes,
    }


async def test_nonce_unique_and_expiry_pair_are_enforced_by_postgresql(nonce_sessions):
    _, plain = nonce_sessions
    values = log()
    async with plain() as db:
        await db.execute(insert(SysJobLog).values(**values))
        async with db.begin_nested():
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(
                        insert(SysJobLog).values(**(values | {"job_log_id": 402}))
                    )
        for changes in ({"dispatch_id": None}, {"dispatch_expires_at": None}):
            with pytest.raises(IntegrityError):
                async with db.begin_nested():
                    await db.execute(
                        insert(SysJobLog).values(
                            **(values | changes | {"job_log_id": 403})
                        )
                    )
        await db.commit()


@pytest.mark.parametrize(
    "status,seconds,protected",
    [("1", 30, True), ("3", -30, True), ("4", -30, True), ("2", -30, False)],
)
async def test_cleanup_cannot_erase_live_nonce_or_uncertain_execution(
    nonce_sessions, status, seconds, protected
):
    factory, plain = nonce_sessions
    values = log(
        status=status,
        dispatch_expires_at=datetime.now(UTC) + timedelta(seconds=seconds),
    )
    async with plain() as db:
        await db.execute(insert(SysJobLog).values(**values))
        await db.commit()
    job, entry = (
        ResourceKey(ResourceKind.JOB, 0, 301),
        ResourceKey(ResourceKind.JOB_LOG, 0, 401),
    )
    async with factory() as db:
        try:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                await tx.lock(LockPlan(existing=(job, entry)))
                await tx.delete_job_fact(
                    entry, expected={"job_id": 301, "status": status}
                )
            if protected:
                pytest.fail("Protected dispatch lineage deleted")
            await db.commit()
        except BusinessException as error:
            assert protected and error.error_code == "JOB_DISPATCH_PROTECTED"
            await db.rollback()
    async with plain() as db:
        assert bool(await db.scalar(select(SysJobLog.job_log_id))) == protected
