"""Read completion boundary: no ORM or live transaction escapes to HTTP."""

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.db.authorization_owner import rollback_authorization_session
from app.db.authorization_plan_session import _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.modules.auth.service import AccessClaims
from app.modules.system.service.authorization_read_budget import (
    AuthorizationReadBudget,
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.user_read_service import user_read_service


class UserReadOwner:
    async def execute(
        self,
        db,
        *,
        tenant=None,
        actor_user_version=None,
        operation,
        query=None,
        list_claims=None,
    ):
        require_candidate_session(db)
        if list_claims is not None and (
            type(list_claims) is not AccessClaims
            or operation != "list"
            or tenant is not None
            or actor_user_version is not None
        ):
            raise _order_error()
        tenant_id = tenant.tenant_id if list_claims is None else list_claims.tenant_id
        try:
            with bounded_authorization_reads(
                db, AuthorizationReadBudget(max_statements=4096)
            ):
                async with authorization_transactions.scope(
                    db,
                    tenants=(
                        TenantLock(
                            tenant_id,
                            LockMode.EXCLUSIVE
                            if operation in {"operation-logs", "login-logs"}
                            else LockMode.SHARED,
                        ),
                    ),
                    budget=TransactionBudget(timeout_ms=8000),
                ) as tx:
                    if list_claims is not None:
                        return await user_read_service.user_list(
                            db, tx, claims=list_claims, query=query
                        )
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
