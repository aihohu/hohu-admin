"""One fresh transaction and one ACK for each fixed job command."""

import asyncio

from sqlalchemy import and_, case, func, select

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.exceptions import BusinessException
from app.core.field_policy.contracts import limit
from app.core.tenant import PlatformContext
from app.db.authorization_owner import (
    authorization_session,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.modules.job.dispatch import verify_dispatch
from app.modules.job.execution_contract import prepare_job_execution
from app.modules.job.models.job import SysJob
from app.modules.job.service.authorization_job_http import job_http_candidate, prepare
from app.modules.job.service.authorization_job_lifecycle import (
    JobRun,
    job_lifecycle_candidate,
)
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.parameter_facts import parameter_rows


class JobOwner:
    def __init__(self, factory):
        self.factory = factory

    async def _tenants(self, platform):
        if (
            type(platform) is not PlatformContext
            or "platform:job:operate" not in platform.permissions
        ):
            raise _order_error()
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db, budget=TransactionBudget(timeout_ms=8000)
                    ):
                        identifiers, last = [], -1
                        while True:
                            batch = list(
                                await db.scalars(
                                    select(Tenant.tenant_id)
                                    .where(
                                        Tenant.status == "1",
                                        Tenant.lifecycle_state == "active",
                                        Tenant.tenant_id > last,
                                    )
                                    .order_by(Tenant.tenant_id)
                                    .limit(256)
                                )
                            )
                            identifiers.extend(batch)
                            limit(len(identifiers) <= 1024)
                            if len(batch) < 256:
                                break
                            last = batch[-1]
            finally:
                await rollback_authorization_session(db)
        return tuple(identifiers)

    async def scheduled(self, *, platform):
        results = []
        async with asyncio.timeout(60):
            identifiers = await self._tenants(platform)
            for tenant_id in identifiers:
                async with authorization_session(self.factory) as db:
                    try:
                        with bounded_authorization_reads(
                            db, AuthorizationReadBudget(max_statements=4096)
                        ):
                            async with authorization_transactions.scope(
                                db,
                                tenants=(TenantLock(tenant_id, LockMode.SHARED),),
                                budget=TransactionBudget(timeout_ms=8000),
                            ):
                                tenant = await db.get(Tenant, tenant_id)
                                if (
                                    tenant is None
                                    or tenant.status != "1"
                                    or tenant.lifecycle_state != "active"
                                ):
                                    continue
                                # Never fetch legacy unknown args or human audit/name fields.
                                table = SysJob.__table__
                                names = (
                                    "job_id",
                                    "tenant_id",
                                    "job_key",
                                    "job_args",
                                    "trigger_type",
                                    "cron_expression",
                                    "interval_value",
                                    "interval_unit",
                                    "status",
                                    "concurrent",
                                    "timeout_seconds",
                                    "max_retries",
                                    "run_on_enable",
                                )
                                args = case(
                                    (table.c.job_args.is_(None), None),
                                    (
                                        and_(
                                            func.pg_catalog.octet_length(
                                                table.c.job_args
                                            )
                                            <= 8192,
                                            table.c.job_args.op("~")(
                                                r"^\s*\{\s*\}\s*$"
                                            ),
                                        ),
                                        table.c.job_args,
                                    ),
                                    else_="!invalid",
                                ).label("job_args")
                                rows = await parameter_rows(
                                    db,
                                    table,
                                    tenant_id,
                                    predicates=(
                                        table.c.status == "1",
                                        table.c.job_key == "test_task",
                                    ),
                                    names=names,
                                    expressions={"job_args": args},
                                )
                                for row in rows:
                                    try:
                                        prepare_job_execution(row)
                                    except BusinessException:
                                        continue
                                    results.append(row)
                                    limit(len(results) <= 10000)
                    finally:
                        await rollback_authorization_session(db)
        return tuple(results)

    async def scan(self, *, platform, runner_id):
        identifiers = await self._tenants(platform)
        count = 0
        for tenant_id in identifiers:
            count += await self._lifecycle("reclaim", tenant_id, runner_id=runner_id)
        return count

    async def claim(self, *, tenant_id=None, job_id=None, runner_id, dispatch=None):
        for value, minimum in ((tenant_id, 0), (job_id, 1)):
            if value is not None and (
                type(value) is not int or not minimum <= value < 2**63
            ):
                raise _order_error()
        if dispatch is not None:
            claims = verify_dispatch(dispatch)
            if (
                tenant_id is not None
                and tenant_id != claims.tenant_id
                or job_id is not None
                and job_id != claims.job_id
            ):
                raise _order_error()
            tenant_id, job_id = claims.tenant_id, claims.job_id
        if type(job_id) is not int or not 0 < job_id < 2**63:
            raise _order_error()
        return await self._lifecycle(
            "claim", tenant_id, job_id=job_id, runner_id=runner_id, dispatch=dispatch
        )

    async def finish(self, claim, *, status, error_msg=None, attempts=1):
        if type(claim) is not JobRun:
            raise _order_error()
        claim.require()
        return await self._lifecycle(
            "finish",
            claim.log["tenant_id"],
            claim=claim,
            status=status,
            error_msg=error_msg,
            attempts=attempts,
        )

    async def _lifecycle(self, action, tenant_id, **request):
        if type(tenant_id) is not int or not 0 <= tenant_id < 2**63:
            raise _order_error()
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(
                    db, AuthorizationReadBudget(max_statements=8192)
                ):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant_id, LockMode.EXCLUSIVE),),
                        budget=TransactionBudget(timeout_ms=15000),
                    ) as tx:
                        result = await getattr(job_lifecycle_candidate, action)(
                            db, tx, tenant_id=tenant_id, **request
                        )
            except BaseException:
                await rollback_authorization_session(db)
                raise
            await commit_authorization_session(db)
            return result

    async def execute(
        self,
        *,
        tenant,
        actor_user_version,
        operation,
        payload=None,
        ids=(),
        days=None,
        http_protocol=False,
        protocol_receipt=None,
    ):
        if type(http_protocol) is not bool:
            raise _order_error()
        command = prepare(operation, payload=payload, ids=ids, days=days)
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(
                    db, AuthorizationReadBudget(max_statements=8192)
                ):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),),
                        budget=TransactionBudget(timeout_ms=15000),
                    ) as tx:
                        result = await job_http_candidate.execute(
                            db,
                            tx,
                            tenant=tenant,
                            actor_user_version=actor_user_version,
                            command=command,
                            http_protocol=http_protocol,
                            protocol_receipt=protocol_receipt,
                        )
            except BaseException:
                await rollback_authorization_session(db)
                raise
            if operation == "run":
                await rollback_authorization_session(db)
            else:
                await commit_authorization_session(db)
            return result
