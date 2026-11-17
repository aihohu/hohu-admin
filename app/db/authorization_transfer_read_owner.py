"""Release G/B before encoding, then re-prove the complete delivery snapshot."""

import asyncio

from app.core.authorization_io import file_transform_worker
from app.core.authorization_plan import TenantLock
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.db.authorization_owner import (
    authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _timeout_error
from app.modules.system.service import user_transfer_read_service as transfers
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.transfer_delivery_snapshot import (
    DeliveryReadBudget,
    remaining_budget,
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
        deadline = asyncio.get_running_loop().time() + 15
        budget = DeliveryReadBudget(max_statements=4096, max_bytes=128 * 1024 * 1024)
        request = {
            "tenant": tenant,
            "actor_user_version": actor_user_version,
            "operation": operation,
            "query": query,
            "identifier": identifier,
        }
        try:
            async with asyncio.timeout_at(deadline):
                result = await self._snapshot(request, budget, deadline)
                if not isinstance(result, transfers.TemplateSnapshot):
                    return result
                payload = await file_transform_worker.run(
                    transfers._build_template, result.departments, result.roles
                )
                if len(payload) > 10 * 1024 * 1024:
                    raise BusinessRuleException(
                        "模板超过同步预算",
                        error_code="AUTHORIZATION_FILE_LIMIT_EXCEEDED",
                    )
                current = await self._snapshot(request, budget, deadline)
                if current != result:
                    raise AuthorizationException(
                        "模板授权或来源已变化", error_code="FIELD_AUTHORIZATION_CHANGED"
                    )
                return payload
        except TimeoutError:
            raise _timeout_error() from None

    async def _snapshot(self, request, budget, deadline):
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db, budget):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(request["tenant"].tenant_id),),
                        budget=remaining_budget(deadline, 15000),
                    ) as tx:
                        return await user_transfer_read_service.execute(
                            db,
                            tx,
                            tenant=request["tenant"],
                            version=request["actor_user_version"],
                            operation=request["operation"],
                            query=request["query"],
                            identifier=request["identifier"],
                        )
            finally:
                await rollback_authorization_session(db)
