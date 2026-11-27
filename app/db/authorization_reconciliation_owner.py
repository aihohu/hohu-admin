"""Maintenance evidence and transaction ownership for legacy annotations."""

import json

from app.core.authorization_maintenance import require_maintenance
from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.db.authorization_owner import (
    authorization_session,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.modules.ai.service.authorization_reconciliation_candidate import (
    reconciliation_candidate,
    validate_ids,
)
from app.modules.ai.service.legacy_reconciliation_service import _invalid
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


class ReconciliationOwner:
    def __init__(self, factory):
        self.factory = factory

    async def inspect(self, *, tenant_id, operator_id, log_ids):
        return await self._execute(tenant_id, operator_id, validate_ids(log_ids))

    async def annotate(
        self,
        *,
        tenant_id,
        operator_id,
        manifest,
        reason,
        evidence_sha256,
        maintenance_sha256,
        fence,
    ):
        evidence = require_maintenance(fence, "ai.reconcile")
        if (
            maintenance_sha256 != evidence.manifest_digest
            or type(manifest) is not list
            or not 1 <= len(manifest) <= 100
        ):
            raise _invalid()
        encoded = json.dumps(manifest)
        if len(encoded.encode()) > 1024 * 1024:
            raise _invalid()
        manifest = json.loads(encoded)
        try:
            if any(item["tenantId"] != str(tenant_id) for item in manifest):
                raise _invalid()
            ids = validate_ids([int(item["logId"]) for item in manifest])
            annotation = {
                int(item["logId"]): (
                    item["baseline"],
                    reason,
                    evidence_sha256,
                    maintenance_sha256,
                )
                for item in manifest
            }
        except (KeyError, TypeError, ValueError):
            raise _invalid() from None
        return await self._execute(
            tenant_id, operator_id, ids, annotation=annotation, fence=fence
        )

    async def _execute(
        self, tenant_id, operator_id, ids, *, annotation=None, fence=None
    ):
        if (
            type(tenant_id) is not int
            or not 0 <= tenant_id < 2**63
            or type(operator_id) is not int
            or not 0 < operator_id < 2**63
        ):
            raise _invalid()
        async with authorization_session(self.factory) as db:
            try:
                with bounded_authorization_reads(db):
                    async with authorization_transactions.scope(
                        db,
                        tenants=(
                            TenantLock(
                                tenant_id,
                                LockMode.EXCLUSIVE
                                if annotation is not None
                                else LockMode.SHARED,
                            ),
                        ),
                        budget=TransactionBudget(timeout_ms=8000),
                    ) as tx:
                        result = await reconciliation_candidate.execute(
                            db,
                            tx,
                            tenant_id=tenant_id,
                            operator_id=operator_id,
                            log_ids=ids,
                            annotation=annotation,
                        )
                        if annotation is not None:
                            require_maintenance(fence, "ai.reconcile")
            except BaseException:
                await rollback_authorization_session(db)
                raise
            if annotation is None:
                await rollback_authorization_session(db)
            else:
                await commit_authorization_session(db)
            return result
