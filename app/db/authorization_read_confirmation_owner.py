"""One bounded transaction owns read execution and its durable confirmation."""

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.db.authorization_owner import (
    authorization_session,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.modules.ai.service.authorization_read_confirmation import (
    read_confirmation_candidate,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


class ReadConfirmationOwner:
    def __init__(self, factory):
        self.factory = factory

    async def confirm(self, confirmation_id, **request):
        return await self._run(confirmation_id, project=False, **request)

    async def project(self, confirmation_id, **request):
        return await self._run(confirmation_id, project=True, **request)

    async def _run(self, confirmation_id, *, project, **request):
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(
                            TenantLock(request["tenant"].tenant_id, LockMode.SHARED),
                        ),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        if project:
                            result = await read_confirmation_candidate.project(
                                db, tx, confirmation_id, **request
                            )
                        else:
                            result = await read_confirmation_candidate.execute(
                                db, tx, confirmation_id, **request
                            )
                            db.expunge(result)
            except BaseException:
                await rollback_authorization_session(db)
                raise
            if project or request.get("decision") == "read":
                await rollback_authorization_session(db)
            else:
                await commit_authorization_session(db)
            return result
