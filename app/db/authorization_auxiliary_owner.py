"""Short configuration reads finish before auxiliary Redis work starts."""

from app.core.authorization_plan import TenantLock, TransactionBudget
from app.db.authorization_owner import (
    authorization_session,
    rollback_authorization_session,
)
from app.modules.system.service.authorization_auxiliary_candidate import (
    auxiliary_candidate,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


class AuxiliaryOwner:
    def __init__(self, factory):
        self.factory = factory

    async def options(self, *, tenant):
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id),),
                        budget=TransactionBudget(
                            timeout_ms=1000,
                            lock_timeout_ms=500,
                            statement_timeout_ms=500,
                        ),
                    ) as tx:
                        return await auxiliary_candidate.options(db, tx, tenant=tenant)
            finally:
                await rollback_authorization_session(db)
