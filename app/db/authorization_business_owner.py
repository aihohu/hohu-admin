"""Fixed non-authority business transactions, with no ordinary-session fallback."""

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.db.authorization_owner import (
    authorization_session,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.modules.system.service.authorization_business_candidate import (
    business_candidate,
    prepare,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


class BusinessOwner:
    def __init__(self, factory):
        self.factory = factory

    async def execute(
        self, *, tenant, actor_user_version, operation, payload=None, ids=()
    ):
        command = prepare(operation, payload, ids)
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),),
                        budget=TransactionBudget(timeout_ms=15000),
                    ) as tx:
                        result = await business_candidate.execute(
                            db,
                            tx,
                            tenant=tenant,
                            actor_user_version=actor_user_version,
                            command=command,
                        )
            except BaseException:
                await rollback_authorization_session(db)
                raise
            await commit_authorization_session(db)
            return result
