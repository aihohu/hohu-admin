import importlib.util
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import UniqueConstraint

from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User

MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "d8e9f0a1b2c3_add_tenant_principal_foundation.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "tenant_foundation_migration", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_seeds_default_tenant_before_user_backfill():
    migration = _load_migration()
    events: list[tuple[str, tuple, dict]] = []

    def record(name):
        return lambda *args, **kwargs: events.append((name, args, kwargs))

    with (
        patch.object(migration.op, "create_table", record("create_table")),
        patch.object(migration.op, "bulk_insert", record("bulk_insert")),
        patch.object(migration.op, "add_column", record("add_column")),
        patch.object(migration.op, "execute", record("execute")),
        patch.object(migration.op, "alter_column", record("alter_column")),
        patch.object(migration.op, "create_foreign_key", record("create_fk")),
        patch.object(migration.op, "create_index", record("create_index")),
    ):
        migration.upgrade()

    names = [name for name, _args, _kwargs in events]
    assert names[:5] == [
        "create_table",
        "bulk_insert",
        "add_column",
        "execute",
        "alter_column",
    ]
    tenant_rows = events[1][1][1]
    assert tenant_rows == [
        {
            "tenant_id": 0,
            "tenant_code": "default",
            "tenant_name": "Default Tenant",
            "status": "1",
            "row_version": 1,
        }
    ]
    assert events[4][2]["nullable"] is False
    assert str(events[4][2]["server_default"]) == "0"


def test_models_keep_m1_user_uniques_compatible_but_add_tenant_fk():
    tenant_column = User.__table__.c.tenant_id

    assert tenant_column.nullable is False
    # Runtime inserts must receive tenant_id from a trusted TenantContext. The
    # migration-only backfill default is removed from the ORM model.
    assert tenant_column.server_default is None
    assert (
        next(iter(tenant_column.foreign_keys)).target_fullname == "sys_tenant.tenant_id"
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(constraint.columns.keys()) == ("tenant_id", "user_name")
        for constraint in User.__table__.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(constraint.columns.keys()) == ("tenant_code",)
        for constraint in Tenant.__table__.constraints
    )
