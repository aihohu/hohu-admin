import importlib.util
from pathlib import Path

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
