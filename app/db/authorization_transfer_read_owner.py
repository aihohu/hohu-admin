"""Fixed transfer reads complete projection before releasing their G/B snapshot."""

from app.core.authorization_plan import TenantLock, TransactionBudget
from app.db.authorization_owner import (
    authorization_session,
    rollback_authorization_session,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.user_transfer_read_service import (
    user_transfer_read_service,
)


class TransferReadOwner:
    def __init__(self, factory):
        self.factory = factory

    async def execute(
        self, *, tenant, actor_user_version, operation, query=None, identifier=None
    ):
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id),),
                        budget=TransactionBudget(timeout_ms=15000),
                    ) as tx:
                        result = await user_transfer_read_service.execute(
                            db,
                            tx,
                            tenant=tenant,
                            version=actor_user_version,
                            operation=operation,
                            query=query,
                            identifier=identifier,
                        )
            finally:
                await rollback_authorization_session(db)
        return result
