"""One fresh transaction and one ACK for each fixed job command."""

from sqlalchemy import select

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.tenant import PlatformContext
from app.db.authorization_owner import (
    authorization_session,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.modules.job.service.authorization_job_http import job_http_candidate, prepare
from app.modules.job.service.authorization_job_lifecycle import (
    JobRun,
    job_lifecycle_candidate,
)
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


class JobOwner:
    def __init__(self, factory):
        self.factory = factory

    async def scan(self, *, platform, runner_id):
        if (
            type(platform) is not PlatformContext
            or "platform:job:operate" not in platform.permissions
        ):
            raise _order_error()
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db, budget=TransactionBudget(timeout_ms=2000)
                    ):
                        identifiers = tuple(
                            await db.scalars(
                                select(Tenant.tenant_id)
                                .where(Tenant.status == "1")
                                .limit(1001)
                            )
                        )
                        if len(identifiers) > 1000:
                            raise _order_error()
            finally:
                await rollback_authorization_session(db)
        count = 0
        for tenant_id in identifiers:
            count += await self._lifecycle("reclaim", tenant_id, runner_id=runner_id)
        return count

    async def claim(self, *, tenant_id, job_id, runner_id, manual=False):
        if type(job_id) is not int or not 0 < job_id < 2**63:
            raise _order_error()
        return await self._lifecycle(
            "claim", tenant_id, job_id=job_id, runner_id=runner_id, manual=manual
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
                with bounded_authorization_reads(db):
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
        self, *, tenant, actor_user_version, operation, payload=None, ids=(), days=None
    ):
        command = prepare(operation, payload=payload, ids=ids, days=days)
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
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
                        )
            except BaseException:
                await rollback_authorization_session(db)
                raise
            if operation == "run":
                await rollback_authorization_session(db)
            else:
                await commit_authorization_session(db)
            return result
