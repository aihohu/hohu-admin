"""Dual-tenant scheduler checks use an owned disposable schema."""

import pytest

from app.core.exceptions import NotFoundException
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.job import job_runner
from app.modules.job.models.job import SysJob
from app.modules.job.service.job_service import job_service
from app.modules.job.task_registry import _task_functions
from app.modules.system.models.tenant import Tenant
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.tenant_helpers import tenant_context


async def test_job_lookup_is_scoped_and_disabled_tenant_worker_fails_closed(
    system_sessions, monkeypatch
):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    monkeypatch.setattr(job_runner, "get_authorization_runtime", lambda: runtime)
    calls = []

    async def task():
        calls.append(True)

    key = "TEST_TENANT_JOB_DISABLED"
    monkeypatch.setitem(_task_functions, key, task)
    async with plain() as db:
        tenant = await db.get(Tenant, 1)
        job = SysJob(
            tenant_id=1,
            job_name="Tenant B job",
            job_key=key,
            cron_expression="0 * * * *",
            status="1",
            concurrent="1",
        )
        db.add(job)
        await db.flush()
        with pytest.raises(NotFoundException):
            await job_service.get_by_id(db, job.job_id, tenant=tenant_context())
        tenant.status, tenant.lifecycle_state = "2", "disabled"
        await db.commit()
        identifier = job.job_id
    await job_runner._do_execute(1, identifier)
    assert not calls
