"""Only explicit tenant-scoped audit rows can be removed by retention."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import insert, select, update

from app.core.exceptions import BusinessException
from app.modules.platform.authorization_principal_candidate import PrincipalIdentity
from app.modules.platform.authorization_retention_owner import retention_candidate_owner
from app.modules.platform.models import PlatformPrincipal
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from tests.modules.platform.test_authorization_principal_candidate import (
    principal_sessions as principal_sessions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_retention_explicit_rows_preserve_platform_and_epoch(principal_sessions):
    factory, plain = principal_sessions
    cutoff = datetime.now() - timedelta(days=400)
    async with plain() as db:
        await db.execute(
            update(PlatformPrincipal).values(permissions=["platform:audit:retention"])
        )
        for log_id, scope in ((900, "tenant"), (901, "platform")):
            await db.execute(
                insert(SysOperationLog).values(
                    operation_log_id=log_id,
                    tenant_id=1,
                    audit_scope=scope,
                    user_id=102,
                    username="test",
                    module="test",
                    action="test",
                    method="POST",
                    path="/test",
                    create_time=cutoff - timedelta(days=1),
                )
            )
        await db.commit()
    async with factory() as db:
        result = await retention_candidate_owner.purge(
            db,
            identity=PrincipalIdentity(700, 2),
            target_tenant_id=1,
            cutoff=cutoff,
            expected_operation_count=1,
            expected_login_count=0,
            reason="retention test",
            ticket_id="R-1",
            correlation_id="retention-test",
            audit_factory=factory,
        )
        assert result.operation_count == 1
    async with plain() as db:
        assert (
            await db.execute(select(SysOperationLog.operation_log_id))
        ).scalars().all() == [901]
        assert (
            await db.execute(select(UserFieldPolicyState.epoch))
        ).scalars().all() == [1, 1]


async def test_retention_stale_counts_preserve_rows(principal_sessions):
    factory, plain = principal_sessions
    async with plain() as db:
        await db.execute(
            update(PlatformPrincipal).values(permissions=["platform:audit:retention"])
        )
        await db.commit()
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await retention_candidate_owner.purge(
                db,
                identity=PrincipalIdentity(700, 2),
                target_tenant_id=1,
                cutoff=datetime.now() - timedelta(days=400),
                expected_operation_count=1,
                expected_login_count=0,
                reason="retention test",
                ticket_id="R-2",
                correlation_id="retention-stale",
                audit_factory=factory,
            )
        assert error.value.error_code == "PLATFORM_RETENTION_PREVIEW_STALE"
