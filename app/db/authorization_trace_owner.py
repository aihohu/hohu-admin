"""Trace read owners return DTOs only after ending their protected snapshot."""

from app.core.authorization_plan import TenantLock, TransactionBudget
from app.db.authorization_owner import rollback_authorization_session
from app.modules.ai.service.authorization_trace_candidate import trace_candidate
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


class TraceOwner:
    def __init__(self, factory):
        self.factory = factory

    async def list(self, **request):
        return await self._read(request, detail=False)

    async def detail(self, **request):
        return await self._read(request, detail=True)

    async def _read(self, request, *, detail):
        async with self.factory() as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(request["tenant"].tenant_id),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        if detail:
                            return await trace_candidate.detail(db, tx, **request)
                        return await trace_candidate.list(db, tx, **request)
            finally:
                await rollback_authorization_session(db)
