"""Audited catalog GETs with bounded DNS between two complete transactions."""

import asyncio

from app.core.authorization_plan import LockPlan, TenantLock, TransactionBudget
from app.core.tenant import PlatformContext
from app.db.authorization_control_owner import (
    _rollback_preserving,
    control_candidate_owner,
)
from app.db.authorization_owner import (
    commit_authorization_session,
    rollback_authorization_session,
)
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.service.authorization_catalog_inputs import stale
from app.modules.platform.authorization_catalog_read import catalog_read_candidate
from app.modules.system.service.authorization_control_audit import consume_control_audit
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)


class CatalogReadOwner:
    def __init__(self, factory):
        self.factory = factory

    async def execute(
        self,
        *,
        tenant,
        actor_user_version,
        operation,
        issued_audit,
        payload=None,
        target_id=None,
        provider_id=None,
        target_tenant_id=None,
    ):
        permission = (
            "platform:tenant:read"
            if operation.startswith("tenant.")
            else "platform:ai:read"
        )
        audit = issued_audit
        audit.assert_request(
            tenant=tenant,
            actor_user_version=actor_user_version,
            operation=operation,
            permission=permission,
            target_tenant_id=target_tenant_id,
        )
        consume_control_audit(audit)
        platform = PlatformContext(
            tenant.actor_user_id,
            audit.actor_name,
            "human",
            frozenset({permission}),
            audit.reason,
            audit.ticket_id,
            audit.correlation_id,
            target_tenant_id,
        )
        request = {
            "operation": operation,
            "platform": platform,
            "payload": payload,
            "target_id": target_id,
            "provider_id": provider_id,
        }
        expected, checks = None, None
        tenants = tuple(
            TenantLock(tid)
            for tid in sorted(
                {tenant.tenant_id}
                | ({target_tenant_id} if target_tenant_id is not None else set())
            )
        )
        for _phase in range(2):
            async with self.factory() as db:
                try:
                    with bounded_authorization_reads(db):
                        async with authorization_transactions.scope(
                            db,
                            tenants=tenants,
                            budget=TransactionBudget(timeout_ms=8000),
                        ) as tx:
                            identity = await read_identity(
                                db,
                                tenant=tenant,
                                actor_user_version=actor_user_version,
                                permissions=(permission,),
                                system=True,
                            )
                            await tx.lock(
                                LockPlan(
                                    existing=tuple(
                                        set(identity.keys) | set(audit.existing)
                                    ),
                                    new=audit.new,
                                )
                            )
                            await assert_identity(db, identity)
                            result = await catalog_read_candidate.project(
                                db, tx, **request
                            )
                            if expected is not None and result.snapshot() != expected:
                                raise stale()
                            complete = not result.probes or checks is not None
                            if complete:
                                data = result.checked(checks or ())
                                await audit.append(tx, {"recordCount": result.count})
                except BaseException as error:
                    await _rollback_preserving(db, error)
                    await control_candidate_owner.complete_failure(
                        self.factory, audit, error
                    )
                    raise
                if complete:
                    # An unknown ACK never enters the failure-completion path.
                    await commit_authorization_session(db)
                    return data
                await rollback_authorization_session(db)
            expected = result.snapshot()
            semaphore = asyncio.Semaphore(10)

            async def probe(values, semaphore=semaphore):
                async with semaphore:
                    return await provider_egress.is_configuration_allowed(**values)

            try:
                async with asyncio.timeout(5):
                    checks = await asyncio.gather(
                        *(probe(values) for values in result.probes)
                    )
            except BaseException as error:
                await control_candidate_owner.complete_failure(
                    self.factory, audit, error
                )
                raise
