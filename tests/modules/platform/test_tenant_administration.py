from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.exceptions import AuthorizationException, BusinessException
from app.core.id_generator import next_id
from app.core.tenant import DEFAULT_TENANT_ID, PlatformContext
from app.modules.platform.constants import (
    PLATFORM_TENANT_READ,
    PLATFORM_TENANT_WRITE,
)
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.service.tenant_lifecycle_service import (
    tenant_lifecycle_service,
)


def _platform(permission: str, target_tenant_id: int | None) -> PlatformContext:
    return PlatformContext(
        actor_principal_id=91,
        actor_name="tenant-operator",
        principal_type="human",
        permissions=frozenset({permission}),
        reason="Prepare tenant registry",
        ticket_id="TENANT-91",
        correlation_id=f"tenant-91:{target_tenant_id}",
        target_tenant_id=target_tenant_id,
    )


async def test_prepare_tenant_is_disabled_idempotent_and_has_no_runtime_seed(
    db_session,
):
    tenant_id = next_id()
    code = f"prepared-{tenant_id}"
    platform = _platform(PLATFORM_TENANT_WRITE, tenant_id)

    created = await tenant_lifecycle_service.prepare_tenant(
        db_session,
        tenant_id=tenant_id,
        tenant_code=code,
        tenant_name="Prepared Tenant",
        idempotency_key="tenant-create-00000001",
        platform=platform,
    )
    replay = await tenant_lifecycle_service.prepare_tenant(
        db_session,
        tenant_id=tenant_id,
        tenant_code=code,
        tenant_name="Prepared Tenant",
        idempotency_key="tenant-create-00000001",
        platform=_platform(PLATFORM_TENANT_WRITE, tenant_id),
    )

    assert created.tenant_id == tenant_id
    assert replay.tenant_id == tenant_id
    assert created.status == "2"
    assert created.lifecycle_state == "prepared"
    assert created.provisioning_key_hash != "tenant-create-00000001"
    assert (
        await db_session.scalar(
            select(func.count()).select_from(User).where(User.tenant_id == tenant_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Role).where(Role.tenant_id == tenant_id)
        )
        == 0
    )


async def test_prepare_same_idempotency_key_with_different_payload_conflicts(
    db_session,
):
    tenant_id = next_id()
    platform = _platform(PLATFORM_TENANT_WRITE, tenant_id)
    await tenant_lifecycle_service.prepare_tenant(
        db_session,
        tenant_id=tenant_id,
        tenant_code=f"idem-{tenant_id}",
        tenant_name="Original",
        idempotency_key="tenant-create-00000002",
        platform=platform,
    )

    with pytest.raises(BusinessException) as exc_info:
        await tenant_lifecycle_service.prepare_tenant(
            db_session,
            tenant_id=tenant_id,
            tenant_code=f"different-{tenant_id}",
            tenant_name="Different",
            idempotency_key="tenant-create-00000002",
            platform=_platform(PLATFORM_TENANT_WRITE, tenant_id),
        )

    assert exc_info.value.code == 409
    assert exc_info.value.error_code == "PLATFORM_TENANT_IDEMPOTENCY_CONFLICT"


async def test_disable_tenant_is_idempotent_and_database_bumps_security_version(
    db_session,
):
    tenant_id = next_id()
    tenant = Tenant(
        tenant_id=tenant_id,
        tenant_code=f"active-{tenant_id}",
        tenant_name="Active Tenant",
        status="1",
        lifecycle_state="active",
        row_version=1,
    )
    db_session.add(tenant)
    await db_session.flush()

    disabled = await tenant_lifecycle_service.disable_tenant(
        db_session,
        tenant_id=tenant_id,
        platform=_platform(PLATFORM_TENANT_WRITE, tenant_id),
    )
    await db_session.refresh(tenant)
    first_version = tenant.row_version
    replay = await tenant_lifecycle_service.disable_tenant(
        db_session,
        tenant_id=tenant_id,
        platform=_platform(PLATFORM_TENANT_WRITE, tenant_id),
    )

    assert disabled.lifecycle_state == "disabled"
    assert disabled.status == "2"
    assert first_version == 2
    assert replay.row_version == first_version


async def test_default_tenant_cannot_be_disabled(db_session):
    with pytest.raises(BusinessException) as exc_info:
        await tenant_lifecycle_service.disable_tenant(
            db_session,
            tenant_id=DEFAULT_TENANT_ID,
            platform=_platform(PLATFORM_TENANT_WRITE, DEFAULT_TENANT_ID),
        )

    assert exc_info.value.error_code == "PLATFORM_DEFAULT_TENANT_IMMUTABLE"


async def test_tenant_service_rechecks_permission_before_database_access():
    db = AsyncMock()
    with pytest.raises(AuthorizationException) as exc_info:
        await tenant_lifecycle_service.list_tenants(
            db,
            current=1,
            size=20,
            platform=_platform(PLATFORM_TENANT_WRITE, None),
        )

    assert exc_info.value.error_code == "PLATFORM_PERMISSION_DENIED"
    db.execute.assert_not_awaited()
    db.scalar.assert_not_awaited()


async def test_get_tenant_rejects_context_for_a_different_target(db_session):
    with pytest.raises(AuthorizationException) as exc_info:
        await tenant_lifecycle_service.get_tenant(
            db_session,
            tenant_id=next_id(),
            platform=_platform(PLATFORM_TENANT_READ, next_id()),
        )

    assert exc_info.value.error_code == "PLATFORM_TARGET_TENANT_MISMATCH"
