"""Only explicit TaskSpecs, split commands and live fields authorize job writes."""

import pytest
from sqlalchemy import insert, select

from app.core.exceptions import BusinessException
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.job import task_registry
from app.modules.job.builtin_task_specs import builtin_task_contracts
from app.modules.job.models.job import SysJob
from app.modules.job.schemas.job import JobCreate, JobUpdate
from app.modules.job.service.authorization_job_http import prepare
from app.modules.system.field_policy.ordinary_catalog import (
    production_catalog as control_catalog,
)
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


def definition(**changes):
    return JobCreate(
        job_key="test_task", job_name="Test", cron_expression="0 * * * *", **changes
    )


def test_registering_a_function_does_not_register_a_task_capability(monkeypatch):
    async def uncontracted(_args):
        pass

    monkeypatch.setitem(task_registry._task_functions, "uncontracted", uncontracted)
    monkeypatch.setitem(
        task_registry._task_scopes, "uncontracted", task_registry.TaskScope.TENANT
    )
    assert "uncontracted" not in {
        row["key"] for row in builtin_task_contracts().tenant_descriptions()
    }
    with pytest.raises(BusinessException) as denied:
        prepare(
            "create",
            payload=JobCreate(
                job_key="uncontracted", job_name="Unknown", cron_expression="0 * * * *"
            ),
        )
    assert denied.value.error_code == "JOB_ARGUMENT_SCHEMA_REQUIRED"


@pytest.mark.parametrize(
    "changes",
    [
        {"job_args": '{"tenantId":1}'},
        {"job_args": '{"x":1}'},
        {"job_args": "[]"},
        {"job_args": "not-json"},
        {"job_args": '{"x":"' + ("x" * 8192) + '"}'},
        {"max_retries": 1},
        {"timeout_seconds": 61},
    ],
)
def test_prepare_rejects_unregistered_arguments_and_execution_budgets(changes):
    with pytest.raises(BusinessException):
        prepare("create", payload=definition(**changes))


def test_ordinary_metadata_command_cannot_change_a_schedule_or_status():
    for changes in (
        {"cron_expression": "*/2 * * * *"},
        {"job_args": "{}"},
        {"status": "1"},
        {"run_on_enable": True},
    ):
        with pytest.raises(BusinessException):
            prepare("update", payload=JobUpdate(job_id=301, **changes))


async def test_unknown_historical_task_can_be_renamed_and_disabled_but_not_enabled(
    system_sessions,
):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    async with plain() as db:
        await db.execute(
            insert(SysJob).values(
                tenant_id=0,
                job_id=301,
                job_key="uncontracted",
                job_name="Legacy",
                job_args="unregistered bytes",
                status="2",
            )
        )
        await db.commit()
    await runtime.job.execute(
        tenant=TENANT,
        actor_user_version=1,
        operation="update",
        payload=JobUpdate(job_id=301, remark="repair metadata"),
    )
    with pytest.raises(BusinessException):
        await runtime.job.execute(
            tenant=TENANT,
            actor_user_version=1,
            operation="status",
            payload=JobUpdate(job_id=301, status="1"),
        )
    async with plain() as db:
        row = await db.scalar(select(SysJob).where(SysJob.job_id == 301))
        assert (
            row.remark == "repair metadata"
            and row.job_args == "unregistered bytes"
            and row.status == "2"
        )


@pytest.mark.parametrize(
    "missing,immediate", [(("job_name", "create"), False), (("job_args", "read"), True)]
)
async def test_job_create_checks_complete_footprint_and_immediate_run_sources(
    system_sessions, monkeypatch, missing, immediate
):
    await activate_relations(
        system_sessions,
        monkeypatch,
        catalog=control_catalog(),
        missing=(("job.definition", *missing),),
    )
    runtime = AuthorizationRuntime(system_sessions[1].kw["bind"])
    with pytest.raises(BusinessException) as denied:
        await runtime.job.execute(
            tenant=TENANT,
            actor_user_version=1,
            operation="create",
            payload=definition(
                run_on_enable=immediate, status="1" if immediate else "2"
            ),
        )
    assert denied.value.error_code == "FIELD_POLICY_DENIED"
    async with system_sessions[1]() as db:
        assert list(await db.scalars(select(SysJob.job_id))) == []


async def test_manual_request_requires_current_args_read_before_issuing_dispatch(
    system_sessions, monkeypatch
):
    runtime = AuthorizationRuntime(system_sessions[1].kw["bind"])
    created = await runtime.job.execute(
        tenant=TENANT, actor_user_version=1, operation="create", payload=definition()
    )
    await activate_relations(
        system_sessions,
        monkeypatch,
        catalog=control_catalog(),
        missing=(("job.definition", "job_args", "read"),),
    )
    with pytest.raises(BusinessException) as denied:
        await runtime.job.execute(
            tenant=TENANT,
            actor_user_version=1,
            operation="run",
            ids=(int(created["job_id"]),),
        )
    assert denied.value.error_code == "FIELD_POLICY_DENIED"
