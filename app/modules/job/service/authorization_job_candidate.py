"""Fixed cron mutation composed into the original Gateway transaction."""

from dataclasses import dataclass

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, ResourceKey, ResourceKind
from app.core.scheduler import validate_trigger_config
from app.db.authorization_plan_session import _order_error
from app.modules.job.ai_tools import _planned_cron_result
from app.modules.job.models.job import SysJob
from app.modules.job.schemas.job import JobAiUpdate
from app.modules.job.task_registry import is_tenant_task
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)


@dataclass(frozen=True, slots=True, repr=False)
class _JobInput:
    values: tuple


@dataclass(slots=True, repr=False)
class _JobWork:
    transaction: object
    command: _JobInput
    identity: object
    facts: dict
    locks: LockPlan
    used: bool = False


class JobAuthorizationCandidate:
    def prepare_input(self, *, tenant, actor_user_version, args):
        if (
            set(args) != {"job_id", "cron_expression"}
            or type(args["job_id"]) is not int
            or not 0 < args["job_id"] < 2**63
        ):
            raise _order_error()
        payload = JobAiUpdate.model_validate(args)
        if not payload.cron_expression:
            raise _order_error()
        return _JobInput(
            (tenant, actor_user_version, payload.job_id, payload.cron_expression)
        )

    async def _facts(self, db, command):
        tenant, _, identifier, cron = command.values
        table = SysJob.__table__
        row = (
            (
                await db.execute(
                    select(table).where(
                        table.c.tenant_id == tenant.tenant_id,
                        table.c.job_id == identifier,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or not is_tenant_task(row["job_key"]):
            raise _order_error()
        validate_trigger_config(
            row["trigger_type"], cron, row["interval_value"], row["interval_unit"]
        )
        return dict(row)

    async def plan(self, db, tx, command):
        tx.assert_session(db)
        if type(command) is not _JobInput:
            raise _order_error()
        tenant, version, identifier, _ = command.values
        tx.assert_planning(tenant.tenant_id, exclusive=True)
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=version,
            permissions=("system:job:edit",),
        )
        facts = await self._facts(db, command)
        key = ResourceKey(ResourceKind.JOB, tenant.tenant_id, identifier)
        return _JobWork(
            tx, command, identity, facts, LockPlan(existing=(*identity.keys, key))
        )

    async def apply(self, db, tx, command, work):
        tx.assert_session(db)
        if (
            type(work) is not _JobWork
            or work.transaction is not tx
            or work.command != command
            or work.used
        ):
            raise _order_error()
        work.used = True
        tx.assert_planned(work.locks)
        await assert_identity(db, work.identity)
        if await self._facts(db, command) != work.facts:
            raise _order_error()
        tenant, _, identifier, cron = command.values
        await tx.update_factual(
            ResourceKey(ResourceKind.JOB, tenant.tenant_id, identifier),
            {"cron_expression": cron, "update_by": str(tenant.actor_user_id)},
            expected=work.facts,
        )
        return _planned_cron_result(
            str(identifier), work.facts["cron_expression"] or "", cron
        )


job_authorization_candidate = JobAuthorizationCandidate()
