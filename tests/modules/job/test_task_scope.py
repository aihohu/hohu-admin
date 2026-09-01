"""Tenant schedulers must not act as a platform-operation deputy."""

import pytest

import app.tasks  # noqa: F401 -- imports are the task registration boundary
from app.core.exceptions import BusinessRuleException
from app.core.id_generator import next_id
from app.modules.job.job_runner import _do_execute
from app.modules.job.models.job import SysJob
from app.modules.job.schemas.job import JobCreate, JobQuery
from app.modules.job.service.job_service import job_service
from app.modules.job.task_registry import (
    TaskScope,
    _task_functions,
    _task_scopes,
    get_task_function,
    get_task_scope,
    register_task,
)
from tests.tenant_helpers import tenant_context


class _SessionFactory:
    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *_exc_info):
        return None


@pytest.fixture
def platform_task_key():
    key = f"TEST_PLATFORM_TASK_{next_id()}"
    called = False

    @register_task(key, scope=TaskScope.PLATFORM)
    async def _platform_task():
        nonlocal called
        called = True

    yield key, lambda: called
    _task_functions.pop(key, None)
    _task_scopes.pop(key, None)


async def test_tenant_job_create_cannot_select_platform_task(
    db_session, platform_task_key
):
    key, _called = platform_task_key

    with pytest.raises(BusinessRuleException):
        await job_service.create(
            db_session,
            JobCreate(
                job_name="平台保留任务",
                job_key=key,
                cron_expression="0 * * * *",
            ),
            tenant=tenant_context(),
        )


async def test_tenant_job_runner_fails_closed_for_platform_task(
    db_session, monkeypatch, platform_task_key
):
    key, was_called = platform_task_key
    job = SysJob(
        tenant_id=0,
        job_name="Legacy platform task",
        job_key=key,
        trigger_type="cron",
        cron_expression="0 * * * *",
        status="1",
        concurrent="1",
    )
    db_session.add(job)
    await db_session.flush()
    monkeypatch.setattr(
        "app.modules.job.job_runner.AsyncSessionLocal",
        _SessionFactory(db_session),
    )

    await _do_execute(0, job.job_id)

    assert was_called() is False
    page = await job_service.get_list(db_session, JobQuery(), tenant=tenant_context())
    assert job not in page.records


def test_platform_cleanup_registration_is_explicit_and_executable():
    # Audit retention needs a dedicated PlatformContext runner in Plan 5.  Do not
    # leave wrappers registered that call tenant services whose purge API was removed.
    assert get_task_function("clean_operation_logs") is None
    assert get_task_function("clean_login_logs") is None

    # Transfer retention already has working wrappers, but tenant job APIs and
    # schedulers must never expose or invoke them as a confused deputy.
    assert get_task_scope("clean_expired_import_batches") == TaskScope.PLATFORM
    assert get_task_scope("clean_expired_import_previews") == TaskScope.PLATFORM
    assert get_task_scope("clean_expired_export_tasks") == TaskScope.PLATFORM
