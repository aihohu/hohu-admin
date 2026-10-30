"""Actual job routes join the same barrier protocol as the AI cron writer."""

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, event, select

from app.core.tenant import PlatformContext
from app.db.session import AsyncSessionLocal, engine
from app.modules.job import job_runner, task_registry
from app.modules.job.api import job as job_api
from app.modules.job.log_monitor import JobLogMonitor
from app.modules.job.models.job import SysJob, SysJobLog
from app.modules.job.schemas.job import JobCreate, JobUpdate
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_preview_atomic_completion import authenticated_actor


async def test_job_route_mutations_are_planned_and_publish_after_release(
    completion_case, monkeypatch
):
    case = completion_case
    key = "planned_job_" + uuid4().hex[:12]
    monkeypatch.setitem(task_registry._task_functions, key, lambda: None)
    writes, publications = [], []
    request_db = None

    async def notify():
        publications.append(True)
        assert not request_db.in_transaction()

    monkeypatch.setattr(job_api, "notify_job_changed", notify)

    def capture(_conn, _cursor, sql, _params, context, _many):
        if sql.startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(context.execution_options.get("_authorization_plan_permit"))

    try:
        for operation in ("create", "update", "status", "delete"):
            async with AsyncSessionLocal() as db:
                request_db = db
                actor = await authenticated_actor(db, tenant=case.tenant)
                identifier = await db.scalar(
                    select(SysJob.job_id).where(
                        SysJob.tenant_id == 0, SysJob.job_key == key
                    )
                )
                event.listen(engine.sync_engine, "before_cursor_execute", capture)
                try:
                    request = {"db": db, "_current_user": actor, "tenant": case.tenant}
                    if operation == "create":
                        await job_api.add(
                            JobCreate(
                                job_name=key, job_key=key, cron_expression="0 * * * *"
                            ),
                            **request,
                        )
                    elif operation == "update":
                        await job_api.update(
                            JobUpdate(job_id=identifier, cron_expression="5 * * * *"),
                            **request,
                        )
                    elif operation == "status":
                        await job_api.update_status(identifier, "2", **request)
                    else:
                        await job_api.delete(identifier, **request)
                finally:
                    event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert len(writes) >= 3 and all(permit is not None for permit in writes)
        assert len(publications) == 4
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                delete(SysJob).where(SysJob.tenant_id == 0, SysJob.job_key == key)
            )


async def test_runner_claim_is_atomic_and_task_has_no_live_transaction(
    completion_case, monkeypatch
):
    assert completion_case.tenant.tenant_id == 0
    key = "planned_runner_" + uuid4().hex[:12]
    started, finish = asyncio.Event(), asyncio.Event()
    calls, active, writes = [], set(), []

    async def task():
        calls.append(True)
        started.set()
        await finish.wait()
        assert not active, "task callback retained a database transaction"

    monkeypatch.setitem(task_registry._task_functions, key, task)
    async with AsyncSessionLocal() as db, db.begin():
        job = SysJob(
            tenant_id=0,
            job_name=key,
            job_key=key,
            cron_expression="0 * * * *",
            status="1",
            concurrent="2",
        )
        db.add(job)
        await db.flush()
        identifier = job.job_id

    def begin(connection):
        active.add(connection)

    def done(connection):
        active.discard(connection)

    def capture(_conn, _cursor, sql, _params, context, _many):
        if sql.startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(context.execution_options.get("_authorization_plan_permit"))

    handlers = (
        ("begin", begin),
        ("commit", done),
        ("rollback", done),
        ("before_cursor_execute", capture),
    )
    for name, handler in handlers:
        event.listen(engine.sync_engine, name, handler)
    worker = asyncio.create_task(job_runner._do_execute(0, identifier))
    try:
        async with asyncio.timeout(5):
            await started.wait()
        await job_runner._do_execute(0, identifier)
        finish.set()
        await worker
        assert len(calls) == 1
        assert len(writes) == 2 and all(permit is not None for permit in writes)
        async with AsyncSessionLocal() as db:
            logs = list(
                (
                    await db.scalars(
                        select(SysJobLog).where(
                            SysJobLog.tenant_id == 0, SysJobLog.job_id == identifier
                        )
                    )
                ).all()
            )
            assert len(logs) == 1 and logs[0].status == "1"
    finally:
        finish.set()
        await worker
        for name, handler in handlers:
            event.remove(engine.sync_engine, name, handler)
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                delete(SysJob).where(SysJob.tenant_id == 0, SysJob.job_id == identifier)
            )


async def test_monitor_updates_orphan_with_plan_and_preserves_current_runner(
    completion_case,
):
    assert completion_case.tenant.tenant_id == 0
    key = "planned_monitor_" + uuid4().hex[:12]
    async with AsyncSessionLocal() as db, db.begin():
        job = SysJob(
            tenant_id=0,
            job_name=key,
            job_key=key,
            cron_expression="0 * * * *",
            status="1",
        )
        db.add(job)
        await db.flush()
        identifier = job.job_id
        logs = [
            SysJobLog(
                tenant_id=0,
                job_id=identifier,
                job_name=key,
                job_key=key,
                status="3",
                start_time=datetime.now() - timedelta(hours=2),
                runner_id=runner,
            )
            for runner in ("old_runner", "current_runner")
        ]
        db.add_all(logs)
    writes = []

    def capture(_conn, _cursor, sql, _params, context, _many):
        if sql.startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(context.execution_options.get("_authorization_plan_permit"))

    platform = PlatformContext(
        actor_principal_id=0,
        actor_name="job monitor",
        principal_type="service",
        permissions=frozenset({"platform:job:operate"}),
        reason="scheduled cleanup",
        ticket_id="job-monitor",
        correlation_id="job-monitor",
    )
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await JobLogMonitor(platform=platform, runner_id="current_runner")._scan_once()
        assert len(writes) == 1 and writes[0] is not None
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(SysJobLog.runner_id, SysJobLog.status).where(
                        SysJobLog.tenant_id == 0, SysJobLog.job_id == identifier
                    )
                )
            ).all()
            assert dict(rows) == {"old_runner": "2", "current_runner": "3"}
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                delete(SysJob).where(SysJob.tenant_id == 0, SysJob.job_id == identifier)
            )
