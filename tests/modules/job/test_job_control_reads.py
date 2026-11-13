"""Intrinsic task ceilings preserve real null and never fetch unsupported bytes."""

import pytest
from sqlalchemy import insert, update

from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.job.models.job import SysJob
from app.modules.job.schemas.job import JobQuery
from app.modules.system.field_policy.control_catalog import control_catalog
from tests.modules.system.test_authorization_system_candidate import (
    TENANT,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations


async def test_known_null_is_distinct_from_unknown_or_oversize_args(system_sessions):
    _, plain = system_sessions
    async with plain() as db:
        for identifier, key, args in (
            (301, "test_task", None),
            (302, "uncontracted", '{"private":"never deliver"}'),
        ):
            await db.execute(
                insert(SysJob).values(
                    tenant_id=0,
                    job_id=identifier,
                    job_key=key,
                    job_name="Name",
                    job_args=args,
                    status="2",
                )
            )
        await db.commit()
    runtime = AuthorizationRuntime(plain.kw["bind"])
    result = await runtime.parameter_read(
        tenant=TENANT, actor_user_version=1, operation="job.list", query=JobQuery()
    )
    rows = {row["jobId"]: row for row in result["records"]}
    assert "jobArgs" in rows["301"] and rows["301"]["jobArgs"] is None
    assert "jobArgs" not in rows["302"]
    assert rows["301"]["fieldPolicy"]["controlActions"]["canRun"] is True
    assert rows["302"]["fieldPolicy"]["controlActions"]["canRun"] is False
    async with plain() as db:
        await db.execute(
            update(SysJob)
            .where(SysJob.job_id == 301)
            .values(job_args=(" " * 8192) + "{}")
        )
        await db.commit()
    result = await runtime.parameter_read(
        tenant=TENANT, actor_user_version=1, operation="job.list", query=JobQuery()
    )
    assert all("jobArgs" not in row for row in result["records"])


@pytest.mark.parametrize("hidden", [None, "cron_expression", "status"])
async def test_derived_next_run_requires_all_readable_sources(
    system_sessions, monkeypatch, hidden
):
    _, plain = system_sessions
    async with plain() as db:
        await db.execute(
            insert(SysJob).values(
                tenant_id=0,
                job_id=301,
                job_key="test_task",
                job_name="Name",
                cron_expression="0 * * * *",
                status="1",
            )
        )
        await db.commit()
    await activate_relations(
        system_sessions,
        monkeypatch,
        catalog=control_catalog(),
        missing=(("job.definition", hidden, "read"),) if hidden else (),
    )
    result = await AuthorizationRuntime(plain.kw["bind"]).parameter_read(
        tenant=TENANT, actor_user_version=1, operation="job.list", query=JobQuery()
    )
    row = result["records"][0]
    assert ("nextRunTime" in row) == (hidden is None)
    if hidden is None:
        assert row["nextRunTime"] is not None


async def test_special_schedule_fields_are_advisory_and_separate_from_metadata(
    system_sessions, monkeypatch
):
    await activate_relations(
        system_sessions,
        monkeypatch,
        catalog=control_catalog(),
        missing=(("job.definition", "job_args", "update"),),
    )
    result = await AuthorizationRuntime(system_sessions[1].kw["bind"]).parameter_read(
        tenant=TENANT, actor_user_version=1, operation="job.list", query=JobQuery()
    )
    policy = result["fieldPolicy"]
    assert (
        "cron_expression" in policy["specialUpdate"]
        and "status" in policy["specialUpdate"]
    )
    assert "job_args" not in policy["specialUpdate"]
    assert "cron_expression" not in policy["update"]
