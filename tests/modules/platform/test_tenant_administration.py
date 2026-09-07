import hashlib
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.exceptions import AuthorizationException, BusinessException
from app.core.id_generator import next_id
from app.core.tenant import DEFAULT_TENANT_ID, PlatformContext
from app.modules.platform.constants import (
    PLATFORM_TENANT_ACTIVATE,
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
        bootstrap_version=1,
        bootstrap_key_hash=hashlib.sha256(
            f"tenant-admin-key:{tenant_id}".encode()
        ).hexdigest(),
        bootstrap_fingerprint=hashlib.sha256(
            f"tenant-admin-fingerprint:{tenant_id}".encode()
        ).hexdigest(),
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


async def test_bootstrapped_prepared_tenant_can_be_activated_once(
    db_session, monkeypatch
):
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    tenant_id = next_id()
    monkeypatch.setattr(settings, "TENANT_HOSTED_CANARY_TENANT_ID", tenant_id)
    tenant = Tenant(
        tenant_id=tenant_id,
        tenant_code=f"activate-{tenant_id}",
        tenant_name="Activation Candidate",
        status="2",
        lifecycle_state="prepared",
        bootstrap_version=1,
        bootstrap_key_hash=hashlib.sha256(
            f"activation-key:{tenant_id}".encode()
        ).hexdigest(),
        bootstrap_fingerprint=hashlib.sha256(
            f"activation-fingerprint:{tenant_id}".encode()
        ).hexdigest(),
        row_version=1,
    )
    db_session.add(tenant)
    await db_session.flush()
    platform = _platform(PLATFORM_TENANT_ACTIVATE, tenant_id)

    activated = await tenant_lifecycle_service.activate_tenant(
        db_session, tenant_id=tenant_id, platform=platform
    )
    first_version = activated.row_version
    replay = await tenant_lifecycle_service.activate_tenant(
        db_session, tenant_id=tenant_id, platform=platform
    )

    assert activated.status == "1"
    assert activated.lifecycle_state == "active"
    assert first_version == 2
    assert replay.row_version == first_version


async def test_activation_gate_fails_before_database_access(monkeypatch):
    monkeypatch.setattr(settings, "TENANT_MODE", "single")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", False)
    tenant_id = next_id()
    db = AsyncMock()

    with pytest.raises(BusinessException) as exc_info:
        await tenant_lifecycle_service.activate_tenant(
            db,
            tenant_id=tenant_id,
            platform=_platform(PLATFORM_TENANT_ACTIVATE, tenant_id),
        )

    assert exc_info.value.error_code == "PLATFORM_TENANT_ACTIVATION_DISABLED"
    db.scalar.assert_not_awaited()
    db.execute.assert_not_awaited()


async def test_activation_rejects_non_canary_target_before_database_access(
    monkeypatch,
):
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    monkeypatch.setattr(settings, "TENANT_HOSTED_CANARY_TENANT_ID", 22)
    db = AsyncMock()

    with pytest.raises(BusinessException) as exc_info:
        await tenant_lifecycle_service.activate_tenant(
            db,
            tenant_id=23,
            platform=_platform(PLATFORM_TENANT_ACTIVATE, 23),
        )

    assert exc_info.value.error_code == "PLATFORM_TENANT_CANARY_NOT_ALLOWED"
    db.scalar.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("bootstrap_version", "lifecycle_state", "expected_error"),
    [
        (0, "prepared", "PLATFORM_TENANT_NOT_BOOTSTRAPPED"),
        (1, "disabled", "PLATFORM_TENANT_REACTIVATION_UNSUPPORTED"),
    ],
)
async def test_activation_rejects_unready_or_previously_disabled_tenant(
    db_session,
    monkeypatch,
    bootstrap_version,
    lifecycle_state,
    expected_error,
):
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    tenant_id = next_id()
    monkeypatch.setattr(settings, "TENANT_HOSTED_CANARY_TENANT_ID", tenant_id)
    bootstrapped = bootstrap_version == 1
    tenant = Tenant(
        tenant_id=tenant_id,
        tenant_code=f"reject-{tenant_id}",
        tenant_name="Rejected Activation",
        status="2",
        lifecycle_state=lifecycle_state,
        bootstrap_version=bootstrap_version,
        bootstrap_key_hash=(
            hashlib.sha256(f"reject-key:{tenant_id}".encode()).hexdigest()
            if bootstrapped
            else None
        ),
        bootstrap_fingerprint=(
            hashlib.sha256(f"reject-fingerprint:{tenant_id}".encode()).hexdigest()
            if bootstrapped
            else None
        ),
        row_version=1,
    )
    db_session.add(tenant)
    await db_session.flush()

    with pytest.raises(BusinessException) as exc_info:
        await tenant_lifecycle_service.activate_tenant(
            db_session,
            tenant_id=tenant_id,
            platform=_platform(PLATFORM_TENANT_ACTIVATE, tenant_id),
        )

    assert exc_info.value.error_code == expected_error
    await db_session.refresh(tenant)
    assert tenant.status == "2"
    assert tenant.lifecycle_state == lifecycle_state
    assert tenant.row_version == 1


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
