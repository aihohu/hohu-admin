"""Principal retention owners keep intent durable and success atomic."""

import asyncio

from app.core.exceptions import BusinessException
from app.db.authorization_control_owner import _rollback_preserving
from app.db.authorization_owner import commit_authorization_session
from app.db.authorization_plan_session import _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.modules.platform.authorization_principal_owner import (
    _factory,
    principal_candidate_owner,
)
from app.modules.system.service.authorization_retention_candidate import (
    retention_authorization_candidate,
)


class RetentionCandidateOwner:
    async def _execute(
        self,
        db,
        *,
        identity,
        target_tenant_id,
        cutoff,
        purge,
        reason,
        ticket_id,
        correlation_id,
        audit_factory=None,
        expected_operation_count=0,
        expected_login_count=0,
    ):
        require_candidate_session(db)
        factory = _factory(db, audit_factory)
        async with factory() as audit_db:
            if audit_db is db:
                raise _order_error()
            audit = await principal_candidate_owner.authorize(
                audit_db,
                identity=identity,
                permission="platform:audit:retention",
                reason=reason,
                ticket_id=ticket_id,
                correlation_id=correlation_id,
                target_tenant_id=target_tenant_id,
                path="/platform/tenants/{tenant_id}/audit-retention/"
                + ("purge" if purge else "preview"),
                method="POST",
                operation="retention.purge" if purge else "retention.preview",
            )
        try:
            result = await retention_authorization_candidate.execute(
                db,
                identity=identity,
                target_tenant_id=target_tenant_id,
                cutoff=cutoff,
                purge=purge,
                audit=audit,
                expected_operation_count=expected_operation_count,
                expected_login_count=expected_login_count,
            )
        except BaseException as error:
            await _rollback_preserving(db, error)
            try:
                await principal_candidate_owner._failure(factory, audit, error)
            except BaseException:
                if isinstance(error, asyncio.CancelledError):
                    raise error
                raise BusinessException(
                    503, "平台完成审计暂不可用", error_code="PLATFORM_AUDIT_UNAVAILABLE"
                ) from error
            raise
        await commit_authorization_session(db)
        return result

    async def purge(self, db, **request):
        return await self._execute(db, purge=True, **request)

    async def preview(self, db, **request):
        return await self._execute(db, purge=False, **request)


retention_candidate_owner = RetentionCandidateOwner()
