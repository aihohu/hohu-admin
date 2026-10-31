"""Read completion boundary: no ORM or live transaction escapes to HTTP."""

from app.core.authorization_plan import TenantLock
from app.db.authorization_owner import rollback_authorization_session
from app.db.authorization_system_owner import require_candidate_session
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.user_read_service import user_read_service


class UserReadOwner:
    async def execute(self, db, *, tenant, actor_user_version, operation, query=None):
        require_candidate_session(db)
        try:
            with bounded_authorization_reads(db):
                async with authorization_transactions.scope(
                    db, tenants=(TenantLock(tenant.tenant_id),)
                ) as tx:
                    result = await user_read_service.execute(
                        db,
                        tx,
                        tenant=tenant,
                        actor_user_version=actor_user_version,
                        operation=operation,
                        query=query,
                    )
        finally:
            await rollback_authorization_session(db)
        return result


user_read_owner = UserReadOwner()
