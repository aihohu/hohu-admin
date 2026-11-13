"""The fixed AI cron candidate uses the same current schedule field contract."""

import pytest

from app.core.authorization_plan import LockMode, TenantLock
from app.core.exceptions import BusinessException
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.job.service.authorization_job_candidate import (
    job_authorization_candidate,
)
from app.modules.system.field_policy.ordinary_catalog import (
    production_catalog as control_catalog,
)
from app.modules.system.service.authorization_read_budget import (
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
from tests.modules.system.test_relation_field_writes import activate_relations


@pytest.mark.parametrize("operation", ["update", "read"])
async def test_ai_cron_requires_current_write_and_before_value_source(
    system_sessions, monkeypatch, operation
):
    factory, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    created = await runtime.job.execute(
        tenant=TENANT, actor_user_version=1, operation="create", payload=definition()
    )
    await activate_relations(
        system_sessions,
        monkeypatch,
        catalog=control_catalog(),
        missing=(("job.definition", "cron_expression", operation),),
    )
    command = job_authorization_candidate.prepare_input(
        tenant=TENANT,
        actor_user_version=1,
        args={"job_id": int(created["job_id"]), "cron_expression": "5 * * * *"},
    )
    async with factory() as db:
        with bounded_authorization_reads(db):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                with pytest.raises(BusinessException) as denied:
                    await job_authorization_candidate.plan(db, tx, command)
                assert denied.value.error_code == "FIELD_POLICY_DENIED"
        await db.rollback()
