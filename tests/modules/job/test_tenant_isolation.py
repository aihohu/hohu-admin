"""Dual-tenant and worker-boundary regressions for scheduled jobs."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.core.id_generator import next_id
from app.modules.job.job_runner import _do_execute
from app.modules.job.models.job import SysJob
from app.modules.job.service.job_service import job_service
from app.modules.job.task_registry import _task_functions
from tests.tenant_helpers import create_test_tenant, tenant_context


class _SessionFactory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *_exc_info):
        return None


async def test_job_lookup_is_scoped_and_disabled_tenant_worker_fails_closed(
    db_session, monkeypatch
):
    tenant_b = await create_test_tenant(db_session, prefix="job-b")
    task_key = f"TEST_TENANT_JOB_{next_id()}"
    called = False

    async def _task():
        nonlocal called
        called = True

    _task_functions[task_key] = _task
    try:
        job_b = SysJob(
            tenant_id=tenant_b.tenant_id,
            job_name="Tenant B job",
            job_key=task_key,
            trigger_type="cron",
            cron_expression="0 * * * *",
            status="1",
            concurrent="1",
        )
        db_session.add(job_b)
        await db_session.flush()

        with pytest.raises(NotFoundException):
            await job_service.get_by_id(
                db_session, job_b.job_id, tenant=tenant_context()
            )

        tenant_b.status = "2"
        tenant_b.lifecycle_state = "disabled"
        await db_session.flush()
        monkeypatch.setattr(
            "app.modules.job.job_runner.AsyncSessionLocal",
            _SessionFactory(db_session),
        )
        await _do_execute(tenant_b.tenant_id, job_b.job_id)
        assert called is False
    finally:
        _task_functions.pop(task_key, None)
