from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.exceptions import BusinessException
from app.core.id_generator import next_id
from app.core.tenant import PlatformContext
from app.modules.platform.constants import PLATFORM_AUDIT_RETENTION
from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.tenant_support_service import tenant_support_service


def _retention_context(tenant_id: int) -> PlatformContext:
    return PlatformContext(
        actor_principal_id=93,
        actor_name="retention-operator",
        principal_type="human",
        permissions=frozenset({PLATFORM_AUDIT_RETENTION}),
        reason="Apply approved audit retention",
        ticket_id="RETENTION-93",
        correlation_id=f"retention-93:{tenant_id}",
        target_tenant_id=tenant_id,
    )


async def _seed_retention_rows(db_session, tenant_id: int, other_tenant_id: int):
    old = datetime.now() - timedelta(days=100)
    recent = datetime.now() - timedelta(days=10)
    db_session.add_all(
        [
            SysOperationLog(
                tenant_id=tenant_id,
                audit_scope="tenant",
                user_id=1,
                username="old",
                module="system",
                action="read",
                method="GET",
                path="/old",
                create_time=old,
            ),
            SysOperationLog(
                tenant_id=tenant_id,
                audit_scope="tenant",
                user_id=1,
                username="recent",
                module="system",
                action="read",
                method="GET",
                path="/recent",
                create_time=recent,
            ),
            SysOperationLog(
                tenant_id=other_tenant_id,
                audit_scope="tenant",
                user_id=2,
                username="other",
                module="system",
                action="read",
                method="GET",
                path="/other",
                create_time=old,
            ),
            SysLoginLog(
                tenant_id=tenant_id,
                audit_scope="tenant",
                username="old",
                status="1",
                login_time=old,
            ),
            SysLoginLog(
                tenant_id=None,
                audit_scope="unresolved",
                username=f"unresolved-{tenant_id}",
                status="2",
                login_time=old,
            ),
        ]
    )
    await db_session.flush()


async def test_retention_preview_and_compare_delete_are_strictly_tenant_scoped(
    db_session,
):
    tenant_id = next_id()
    other_tenant_id = next_id()
    db_session.add_all(
        [
            Tenant(
                tenant_id=tenant_id,
                tenant_code=f"retention-{tenant_id}",
                tenant_name="Retention Target",
                status="2",
                lifecycle_state="prepared",
            ),
            Tenant(
                tenant_id=other_tenant_id,
                tenant_code=f"retention-{other_tenant_id}",
                tenant_name="Other Target",
                status="2",
                lifecycle_state="prepared",
            ),
        ]
    )
    await db_session.flush()
    await _seed_retention_rows(db_session, tenant_id, other_tenant_id)
    cutoff = datetime.now(UTC) - timedelta(days=95)
    platform = _retention_context(tenant_id)

    preview = await tenant_support_service.preview_retention(
        db_session,
        tenant_id=tenant_id,
        cutoff=cutoff,
        platform=platform,
    )
    result = await tenant_support_service.purge_retention(
        db_session,
        tenant_id=tenant_id,
        cutoff=cutoff,
        expected_operation_count=preview.operation_count,
        expected_login_count=preview.login_count,
        platform=platform,
    )

    assert preview.operation_count == 1
    assert preview.login_count == 1
    assert result.affected_count == 2
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(SysOperationLog)
            .where(SysOperationLog.tenant_id == tenant_id)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(SysOperationLog)
            .where(SysOperationLog.tenant_id == other_tenant_id)
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(SysLoginLog)
            .where(
                SysLoginLog.audit_scope == "unresolved",
                SysLoginLog.username == f"unresolved-{tenant_id}",
            )
        )
        == 1
    )


async def test_retention_stale_preview_deletes_nothing(db_session):
    tenant_id = next_id()
    other_tenant_id = next_id()
    db_session.add_all(
        [
            Tenant(
                tenant_id=tenant_id,
                tenant_code=f"stale-{tenant_id}",
                tenant_name="Stale Target",
                status="2",
                lifecycle_state="prepared",
            ),
            Tenant(
                tenant_id=other_tenant_id,
                tenant_code=f"stale-{other_tenant_id}",
                tenant_name="Other Target",
                status="2",
                lifecycle_state="prepared",
            ),
        ]
    )
    await db_session.flush()
    await _seed_retention_rows(db_session, tenant_id, other_tenant_id)
    cutoff = datetime.now() - timedelta(days=95)

    with pytest.raises(BusinessException) as exc_info:
        await tenant_support_service.purge_retention(
            db_session,
            tenant_id=tenant_id,
            cutoff=cutoff,
            expected_operation_count=0,
            expected_login_count=1,
            platform=_retention_context(tenant_id),
        )

    assert exc_info.value.code == 409
    assert exc_info.value.error_code == "PLATFORM_RETENTION_PREVIEW_STALE"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(SysOperationLog)
            .where(SysOperationLog.tenant_id == tenant_id)
        )
        == 2
    )


async def test_retention_rejects_cutoff_inside_minimum_window(db_session):
    tenant_id = next_id()
    with pytest.raises(BusinessException) as exc_info:
        await tenant_support_service.preview_retention(
            db_session,
            tenant_id=tenant_id,
            cutoff=datetime.now() - timedelta(days=30),
            platform=_retention_context(tenant_id),
        )

    assert exc_info.value.error_code == "PLATFORM_RETENTION_WINDOW_INVALID"
