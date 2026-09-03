import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import inspect, update
from sqlalchemy.exc import DBAPIError

from app.core.id_generator import next_id
from app.core.security import get_password_hash
from app.modules.platform.constants import PLATFORM_TENANT_WRITE
from app.modules.platform.models import PlatformAuditLog, PlatformPrincipal
from app.modules.system.models.tenant import Tenant


def _load_plan5b_migration():
    path = (
        Path(__file__).resolve().parents[3]
        / "alembic"
        / "versions"
        / "3e4f5a6b7c8d_add_platform_tenant_lifecycle.py"
    )
    spec = importlib.util.spec_from_file_location("plan5b_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plan5b_downgrade_refuses_to_erase_prepared_idempotency_state():
    migration = _load_plan5b_migration()
    result = MagicMock()
    result.scalar_one.return_value = True
    connection = MagicMock()
    connection.execute.return_value = result
    migration.op = SimpleNamespace(get_bind=lambda: connection)

    with pytest.raises(RuntimeError, match="PLAN5B_DOWNGRADE_PREPARED_TENANT"):
        migration._assert_downgrade_safe()


def test_plan5b_upgrade_refuses_invalid_legacy_registry_rows():
    migration = _load_plan5b_migration()
    result = MagicMock()
    result.scalar_one.return_value = True
    connection = MagicMock()
    connection.execute.return_value = result
    migration.op = SimpleNamespace(get_bind=lambda: connection)

    with pytest.raises(RuntimeError, match="PLAN5B_TENANT_REGISTRY_INVALID"):
        migration._assert_registry_data_safe()


async def test_plan5b_constraints_and_platform_audit_target_have_expected_shape(
    db_session,
):
    def inspect_schema(connection):
        inspector = inspect(connection)
        tenant_checks = {
            check["name"] for check in inspector.get_check_constraints("sys_tenant")
        }
        tenant_uniques = {
            unique["name"] for unique in inspector.get_unique_constraints("sys_tenant")
        }
        audit_fks = inspector.get_foreign_keys("sys_platform_audit_log")
        audit_checks = {
            check["name"]
            for check in inspector.get_check_constraints("sys_platform_audit_log")
        }
        return tenant_checks, tenant_uniques, audit_fks, audit_checks

    connection = await db_session.connection()
    tenant_checks, tenant_uniques, audit_fks, audit_checks = await connection.run_sync(
        inspect_schema
    )

    assert "ck_sys_tenant_lifecycle_status" in tenant_checks
    assert "ck_sys_tenant_provisioning_hashes" in tenant_checks
    assert "ck_sys_tenant_nonnegative_id" in tenant_checks
    assert "ck_sys_tenant_code_format" in tenant_checks
    assert "ck_sys_tenant_name_format" in tenant_checks
    assert "uq_sys_tenant_provisioning_key_hash" in tenant_uniques
    assert all(
        foreign_key["referred_table"] != "sys_tenant" for foreign_key in audit_fks
    )
    assert "ck_platform_audit_target_tenant_id" in audit_checks


async def test_tenant_lifecycle_changes_bump_version_and_code_is_immutable(db_session):
    tenant_id = next_id()
    tenant = Tenant(
        tenant_id=tenant_id,
        tenant_code=f"lifecycle-{next_id()}",
        tenant_name="Lifecycle Tenant",
        status="1",
        lifecycle_state="active",
        bootstrap_version=1,
        bootstrap_key_hash=hashlib.sha256(
            f"plan5b-key:{tenant_id}".encode()
        ).hexdigest(),
        bootstrap_fingerprint=hashlib.sha256(
            f"plan5b-fingerprint:{tenant_id}".encode()
        ).hexdigest(),
        row_version=1,
    )
    db_session.add(tenant)
    await db_session.flush()

    await db_session.execute(
        update(Tenant)
        .where(Tenant.tenant_id == tenant.tenant_id)
        .values(status="2", lifecycle_state="disabled")
    )
    await db_session.refresh(tenant)
    assert tenant.row_version == 2

    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(Tenant)
                .where(Tenant.tenant_id == tenant.tenant_id)
                .values(tenant_code="renamed-tenant")
            )


async def test_tenant_registry_rejects_partial_hash_pair_and_invalid_code(db_session):
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            db_session.add(
                Tenant(
                    tenant_id=next_id(),
                    tenant_code=f"hash-{next_id()}",
                    tenant_name="Broken Hash Tenant",
                    status="2",
                    lifecycle_state="prepared",
                    provisioning_key_hash="a" * 64,
                    provisioning_fingerprint=None,
                )
            )
            await db_session.flush()

    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            db_session.add(
                Tenant(
                    tenant_id=next_id(),
                    tenant_code="INVALID_CODE",
                    tenant_name="Invalid Code Tenant",
                    status="2",
                    lifecycle_state="prepared",
                )
            )
            await db_session.flush()


async def test_platform_audit_can_preserve_failed_prepare_target(db_session):
    principal = PlatformPrincipal(
        principal_name=f"tenant_auditor_{next_id()}",
        display_name="Tenant Auditor",
        hashed_password=get_password_hash("a-long-test-password1"),
        permissions=[PLATFORM_TENANT_WRITE],
    )
    db_session.add(principal)
    await db_session.flush()
    missing_target_id = next_id()
    event = PlatformAuditLog(
        actor_principal_id=principal.principal_id,
        actor_name=principal.principal_name,
        permission=PLATFORM_TENANT_WRITE,
        event_type="authorized",
        method="POST",
        path="/platform/tenants",
        target_tenant_id=missing_target_id,
        reason="Prepare a tenant",
        ticket_id="TENANT-PREPARE-DB",
        correlation_id=f"tenant-prepare:{missing_target_id}",
    )
    db_session.add(event)
    await db_session.flush()

    assert event.target_tenant_id == missing_target_id
