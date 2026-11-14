"""Fixed cron mutation composed into the original Gateway transaction."""

from dataclasses import dataclass

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, ResourceKey, ResourceKind
from app.core.field_policy.contracts import denied
from app.db.authorization_plan_session import _order_error
from app.modules.job.ai_tools import _planned_cron_result
from app.modules.job.models.job import SysJob
from app.modules.job.schemas.job import JobAiUpdate
from app.modules.job.service.control_job_input import normalize_definition
from app.modules.system.field_policy.control_catalog import UPDATE
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.parameter_context import load_parameter_context


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
        if row is None:
            raise _order_error()
        normalize_definition(dict(row) | {"cron_expression": cron})
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
        context = await load_parameter_context(
            db, tx, tenant=tenant, actor_user_version=version, action="job.schedule"
        )
        context.write(
            "job.definition", identifier, "job.schedule", UPDATE, ("cron_expression",)
        )
        await read_cron_sources(
            db, tx, tenant=tenant, version=version, identifier=identifier
        )
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


async def read_cron_sources(db, tx, *, tenant, version, identifier):
    context = await load_parameter_context(
        db, tx, tenant=tenant, actor_user_version=version, action="job.list"
    )
    fields = ("job_id", "job_name", "cron_expression")
    entries = await context.fetch(
        db,
        SysJob,
        "job_id",
        predicates=(SysJob.job_id == identifier,),
        maximum=1,
        names=fields,
    )
    if not entries:
        raise _order_error()
    record, values, _ = entries[0]
    if not set(fields) <= values.keys():
        raise denied()
    context.source(record, fields)
    return values
