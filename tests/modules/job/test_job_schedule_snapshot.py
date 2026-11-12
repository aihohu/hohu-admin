"""Schedule inventory is bounded, fresh and only exposes real TaskSpecs."""

from sqlalchemy import insert, update

from app.core.tenant import PlatformContext
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.job.models.job import SysJob
from app.modules.system.models.tenant import Tenant
from tests.modules.job.test_job_control_writes import definition
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)

PLATFORM = PlatformContext(
    0,
    "scheduler",
    "service",
    frozenset({"platform:job:operate"}),
    "inventory",
    "test",
    "test",
)


async def test_schedule_snapshot_excludes_unknown_invalid_and_inactive(system_sessions):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    created = await runtime.job.execute(
        tenant=TENANT,
        actor_user_version=1,
        operation="create",
        payload=definition(status="1"),
    )
    async with plain() as db:
        await db.execute(
            insert(SysJob).values(
                tenant_id=0,
                job_id=301,
                job_key="legacy",
                job_name="Secret",
                job_args="secret legacy bytes",
                status="1",
            )
        )
        await db.commit()
    snapshot = await runtime.job.scheduled(platform=PLATFORM)
    assert [row["job_id"] for row in snapshot] == [int(created["job_id"])]
    assert "create_by" not in snapshot[0] and "job_name" not in snapshot[0]
    async with plain() as db:
        await db.execute(
            update(Tenant)
            .where(Tenant.tenant_id == 0)
            .values(status="2", lifecycle_state="disabled")
        )
        await db.commit()
    assert await runtime.job.scheduled(platform=PLATFORM) == ()
