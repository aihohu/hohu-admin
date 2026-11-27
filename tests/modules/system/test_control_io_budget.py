"""Measured owner IO and unusable transactions after a caught budget failure."""

import json
import time

import pytest
from sqlalchemy import insert, select

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.exceptions import BusinessException
from app.db import authorization_job_owner as jobs
from app.db import authorization_parameter_read_owner as reads
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.job.models.job import SysJob
from app.modules.job.schemas.job import JobQuery
from app.modules.system.schemas.config import ConfigQuery
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from tests.modules.job.test_job_control_writes import definition
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_caught_read_budget_exhaustion_cannot_resume_a_job_mutation(
    system_sessions,
):
    factory, plain = system_sessions
    async with plain() as db:
        await db.execute(
            insert(SysJob).values(
                tenant_id=0,
                job_id=301,
                job_key="legacy",
                job_name="Preserve",
                status="2",
            )
        )
        await db.commit()
    key = ResourceKey(ResourceKind.JOB, 0, 301)
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                await tx.lock(
                    LockPlan(existing=(ResourceKey(ResourceKind.TENANT, None, 0), key))
                )
                with pytest.raises(BusinessException):
                    with bounded_authorization_reads(
                        db, AuthorizationReadBudget(max_statements=0)
                    ):
                        await db.scalar(select(SysJob.job_id))
                with pytest.raises(BusinessException):
                    await tx.update_factual(
                        key, {"remark": "forbidden"}, expected={"remark": None}
                    )
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()
    async with plain() as db:
        assert (await db.get(SysJob, 301)).remark is None


async def test_parameter_and_job_owner_measured_io(
    system_sessions, monkeypatch, record_property
):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    measured = []

    def budget(db, value=None):
        current = value or AuthorizationReadBudget()
        measured.append(current)
        return bounded_authorization_reads(db, current)

    monkeypatch.setattr(reads, "bounded_authorization_reads", budget)
    monkeypatch.setattr(jobs, "bounded_authorization_reads", budget)
    created = await runtime.job.execute(
        tenant=TENANT, actor_user_version=1, operation="create", payload=definition()
    )
    durations = []
    for index in range(3):
        started = time.perf_counter()
        for operation, query in (
            ("config.list", ConfigQuery()),
            ("job.list", JobQuery()),
            ("settings.group", "brand"),
        ):
            await runtime.parameter_read(
                tenant=TENANT, actor_user_version=1, operation=operation, query=query
            )
        await runtime.job.execute(
            tenant=TENANT,
            actor_user_version=1,
            operation="update",
            payload={"job_id": int(created["job_id"]), "remark": str(index)},
        )
        durations.append((time.perf_counter() - started) * 1000)
    stats = {
        "maxSql": max(value.statements for value in measured),
        "maxRows": max(value.rows for value in measured),
        "maxDecodedBytes": max(value.decoded_bytes for value in measured),
        "cycleMaxMs": max(durations),
    }
    assert (
        stats["maxSql"] < 1000
        and stats["maxRows"] < 10000
        and stats["maxDecodedBytes"] < 8 * 1024 * 1024
    )
    record_property("control_io", json.dumps(stats))
