import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import inspect, update
from sqlalchemy.exc import DBAPIError

from app.core.id_generator import next_id
from app.modules.system.models.tenant import Tenant


def _load_plan5bb_migration():
    path = (
        Path(__file__).resolve().parents[3]
        / "alembic"
        / "versions"
        / "4f5a6b7c8d9e_add_tenant_bootstrap_marker.py"
    )
    spec = importlib.util.spec_from_file_location("plan5bb_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plan5bb_downgrade_refuses_bootstrapped_non_default_tenant():
    migration = _load_plan5bb_migration()
    result = MagicMock()
    result.scalar_one.return_value = True
    connection = MagicMock()
    connection.execute.return_value = result
    migration.op = SimpleNamespace(get_bind=lambda: connection)

    with pytest.raises(RuntimeError, match="PLAN5BB_DOWNGRADE_BOOTSTRAPPED_TENANT"):
        migration._assert_downgrade_safe()


def test_plan5bb_upgrade_refuses_ambiguous_legacy_active_tenant():
    migration = _load_plan5bb_migration()
    result = MagicMock()
    result.scalar_one.return_value = True
    connection = MagicMock()
    connection.execute.return_value = result
    migration.op = SimpleNamespace(get_bind=lambda: connection)

    with pytest.raises(
        RuntimeError, match="PLAN5BB_LEGACY_ACTIVE_TENANT_REQUIRES_REVIEW"
    ):
        migration._assert_upgrade_safe()


async def test_plan5bb_marker_constraints_and_uniques_have_expected_shape(db_session):
    def inspect_schema(connection):
        inspector = inspect(connection)
        columns = {item["name"] for item in inspector.get_columns("sys_tenant")}
        checks = {
            item["name"] for item in inspector.get_check_constraints("sys_tenant")
        }
        uniques = {
            item["name"] for item in inspector.get_unique_constraints("sys_tenant")
        }
        return columns, checks, uniques

    connection = await db_session.connection()
    columns, checks, uniques = await connection.run_sync(inspect_schema)

    assert {
        "bootstrap_version",
        "bootstrap_key_hash",
        "bootstrap_fingerprint",
    } <= columns
    assert "ck_sys_tenant_bootstrap_state" in checks
    assert "ck_sys_tenant_active_bootstrapped" in checks
    assert "uq_sys_tenant_bootstrap_key_hash" in uniques


async def test_plan5bb_marker_bumps_version_and_partial_state_is_rejected(db_session):
    tenant = Tenant(
        tenant_id=next_id(),
        tenant_code=f"marker-{next_id()}",
        tenant_name="Marker Tenant",
        status="2",
        lifecycle_state="prepared",
        bootstrap_version=0,
        row_version=1,
    )
    db_session.add(tenant)
    await db_session.flush()

    await db_session.execute(
        update(Tenant)
        .where(Tenant.tenant_id == tenant.tenant_id)
        .values(
            bootstrap_version=1,
            bootstrap_key_hash="a" * 64,
            bootstrap_fingerprint="b" * 64,
        )
    )
    await db_session.refresh(tenant)
    assert tenant.row_version == 2

    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            db_session.add(
                Tenant(
                    tenant_id=next_id(),
                    tenant_code=f"partial-{next_id()}",
                    tenant_name="Partial Bootstrap",
                    status="2",
                    lifecycle_state="prepared",
                    bootstrap_version=1,
                    bootstrap_key_hash="c" * 64,
                )
            )
            await db_session.flush()


async def test_active_non_default_tenant_requires_completed_bootstrap(db_session):
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            db_session.add(
                Tenant(
                    tenant_id=next_id(),
                    tenant_code=f"unready-{next_id()}",
                    tenant_name="Unready Active Tenant",
                    status="1",
                    lifecycle_state="active",
                    bootstrap_version=0,
                )
            )
            await db_session.flush()
