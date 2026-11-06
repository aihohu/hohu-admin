"""A direct confirmation proposal commits before any Redis hint is bound."""

import json
from copy import deepcopy
from datetime import datetime

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.db.authorization_owner import (
    authorization_session,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.modules.ai.service.authorization_pending_candidate import pending_candidate
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


class PendingOwner:
    def __init__(self, factory):
        self.factory = factory

    async def reference(self, **request):
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
                        return await pending_candidate.reference(db, tx, **request)
            finally:
                await rollback_authorization_session(db)

    async def prepare(self, **request):
        return await self._proposal(**request)

    async def handoff(self, *, preview_expiry, **request):
        if not isinstance(preview_expiry, datetime):
            raise _order_error()
        return await self._proposal(preview_expiry=preview_expiry, **request)

    async def read_handoff(self, *, preview_expiry, **request):
        if not isinstance(preview_expiry, datetime):
            raise _order_error()
        return await self._proposal(
            preview_expiry=preview_expiry, read_existing=True, **request
        )

    async def _proposal(
        self,
        *,
        tenant,
        actor_user_version,
        log_id,
        original_args_hash,
        scope_hash,
        values,
        preview_expiry=None,
        read_existing=False,
    ):
        values = deepcopy(values)
        if (
            type(values) is not dict
            or "_planned" in values
            or values.get("require_live_source")
        ):
            raise _order_error()
        payload = {key: value for key, value in values.items() if key != "tenant"}

        def encode(value):
            if isinstance(value, datetime):
                return value.isoformat()
            raise TypeError("Invalid proposal input")

        try:
            if (
                len(json.dumps(payload, allow_nan=False, default=encode).encode())
                > 1024 * 1024
                or len(json.dumps(values["frozen_args"], allow_nan=False).encode())
                > 256 * 1024
            ):
                raise _order_error()
        except (ValueError, TypeError, KeyError):
            raise _order_error() from None
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(TenantLock(tenant.tenant_id, LockMode.SHARED),),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        action = await pending_candidate.prepare(
                            db,
                            tx,
                            tenant=tenant,
                            actor_user_version=actor_user_version,
                            log_id=log_id,
                            original_args_hash=original_args_hash,
                            scope_hash=scope_hash,
                            values=values,
                            preview_expiry=preview_expiry,
                            read_existing=read_existing,
                        )
            except BaseException:
                await rollback_authorization_session(db)
                raise
            db.expunge(action)
            if read_existing:
                await rollback_authorization_session(db)
            else:
                await commit_authorization_session(db)
            return action
