"""Support projections and their completion share one bounded planned owner."""

import asyncio

from app.core.authorization_plan import (
    LockPlan,
    ResourceKey,
    ResourceKind,
    TenantLock,
    TransactionBudget,
)
from app.core.exceptions import BusinessException
from app.core.tenant import PlatformContext
from app.db.authorization_control_owner import _rollback_preserving
from app.db.authorization_owner import (
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.modules.platform.authorization_principal_audit import consume_principal_audit
from app.modules.platform.authorization_principal_candidate import (
    assert_principal,
    read_principal,
)
from app.modules.platform.authorization_principal_owner import principal_candidate_owner
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_tenant_candidate import _tenant_row
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.tenant_support_service import tenant_support_service


class SupportCandidateOwner:
    async def execute(
        self,
        db,
        *,
        identity,
        target_tenant_id,
        operation,
        issued_audit,
        audit_factory,
        current=1,
        size=20,
    ):
        require_candidate_session(db)
        if (
            operation not in {"support.operation_logs", "support.login_logs"}
            or type(current) is not int
            or current < 1
            or type(size) is not int
            or not 1 <= size <= 100
        ):
            raise _order_error()
        audit = issued_audit
        audit.assert_request(
            identity=identity,
            permission="platform:support:read",
            target_tenant_id=target_tenant_id,
            operation=operation,
        )
        consume_principal_audit(audit)
        try:
            # Existence is only an error projection, never an authorization proof.
            # A missing tenant has no B row; release this read before entering G/B.
            with bounded_authorization_reads(db):
                async with asyncio.timeout(5):
                    await _tenant_row(db, target_tenant_id)
            await rollback_authorization_session(db)
            with bounded_authorization_reads(db):
                async with authorization_transactions.scope(
                    db,
                    tenants=(TenantLock(target_tenant_id),),
                    budget=TransactionBudget(timeout_ms=8000),
                ) as tx:
                    principal = await read_principal(
                        db, identity, "platform:support:read"
                    )
                    await _tenant_row(db, target_tenant_id)
                    await tx.lock(
                        LockPlan(
                            existing=tuple(
                                set(
                                    principal.keys
                                    + audit.existing
                                    + (
                                        ResourceKey(
                                            ResourceKind.TENANT,
                                            None,
                                            (target_tenant_id,),
                                        ),
                                    )
                                )
                            ),
                            new=audit.new,
                        )
                    )
                    await assert_principal(db, principal)
                    context = PlatformContext(
                        actor_principal_id=identity.principal_id,
                        actor_name=principal.principal_name,
                        principal_type="human",
                        permissions=frozenset(principal.permissions),
                        reason=audit.reason,
                        ticket_id=audit.ticket_id,
                        correlation_id=audit.correlation_id,
                        target_tenant_id=target_tenant_id,
                    )
                    query = (
                        tenant_support_service.list_operation_logs
                        if operation == "support.operation_logs"
                        else tenant_support_service.list_login_logs
                    )
                    result = await query(
                        db,
                        tenant_id=target_tenant_id,
                        current=current,
                        size=size,
                        platform=context,
                    )
                    await audit.append(tx, {"recordCount": len(result.records)})
        except BaseException as error:
            await _rollback_preserving(db, error)
            try:
                await principal_candidate_owner.complete_failure(
                    audit_factory, audit, error
                )
            except BaseException:
                if isinstance(error, asyncio.CancelledError):
                    raise error
                raise BusinessException(
                    503, "平台完成审计暂不可用", error_code="PLATFORM_AUDIT_UNAVAILABLE"
                ) from error
            raise
        await commit_authorization_session(db)
        return result


support_candidate_owner = SupportCandidateOwner()
