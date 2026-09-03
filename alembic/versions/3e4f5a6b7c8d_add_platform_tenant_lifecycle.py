"""Add prepared tenant lifecycle and platform support boundaries.

Revision ID: 3e4f5a6b7c8d
Revises: 2d3e4f5a6b7c
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3e4f5a6b7c8d"
down_revision: str | None = "2d3e4f5a6b7c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CREATE_TENANT_VERSION_FUNCTION = sa.text(
    """
    CREATE FUNCTION bump_sys_tenant_security_version()
    RETURNS trigger AS $$
    BEGIN
        IF NEW.tenant_code IS DISTINCT FROM OLD.tenant_code THEN
            RAISE EXCEPTION 'tenant_code is immutable'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status
           OR NEW.lifecycle_state IS DISTINCT FROM OLD.lifecycle_state THEN
            NEW.row_version := OLD.row_version + 1;
        ELSIF NEW.row_version < OLD.row_version THEN
            RAISE EXCEPTION 'tenant row_version cannot decrease'
                USING ERRCODE = '23514';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
)

_CREATE_TENANT_VERSION_TRIGGER = sa.text(
    """
    CREATE TRIGGER trg_sys_tenant_security_version
    BEFORE UPDATE ON sys_tenant
    FOR EACH ROW EXECUTE FUNCTION bump_sys_tenant_security_version()
    """
)


def _assert_downgrade_safe() -> None:
    has_prepared_state = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM sys_tenant "
                "WHERE lifecycle_state = 'prepared' "
                "OR provisioning_key_hash IS NOT NULL "
                "OR provisioning_fingerprint IS NOT NULL)"
            )
        )
        .scalar_one()
    )
    if has_prepared_state:
        raise RuntimeError("PLAN5B_DOWNGRADE_PREPARED_TENANT")


def _assert_registry_data_safe() -> None:
    invalid_registry = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM sys_tenant "
                "WHERE tenant_id < 0 "
                "OR tenant_code !~ '^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$' "
                "OR btrim(tenant_name) = '' "
                "OR tenant_name ~ '[[:cntrl:]]')"
            )
        )
        .scalar_one()
    )
    if invalid_registry:
        raise RuntimeError("PLAN5B_TENANT_REGISTRY_INVALID")


def upgrade() -> None:
    _assert_registry_data_safe()
    op.add_column(
        "sys_tenant",
        sa.Column(
            "lifecycle_state",
            sa.String(length=16),
            nullable=True,
            comment="active/prepared/disabled",
        ),
    )
    op.add_column(
        "sys_tenant",
        sa.Column(
            "provisioning_key_hash",
            sa.String(length=64),
            nullable=True,
            comment="tenant prepare 幂等键 SHA-256",
        ),
    )
    op.add_column(
        "sys_tenant",
        sa.Column(
            "provisioning_fingerprint",
            sa.String(length=64),
            nullable=True,
            comment="tenant prepare 规范化请求 SHA-256",
        ),
    )
    op.execute(
        sa.text(
            "UPDATE sys_tenant SET lifecycle_state = "
            "CASE WHEN status = '1' THEN 'active' ELSE 'disabled' END"
        )
    )
    op.alter_column("sys_tenant", "lifecycle_state", nullable=False)
    op.create_check_constraint(
        "ck_sys_tenant_nonnegative_id",
        "sys_tenant",
        "tenant_id >= 0",
    )
    op.create_check_constraint(
        "ck_sys_tenant_code_format",
        "sys_tenant",
        "tenant_code ~ '^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$'",
    )
    op.create_check_constraint(
        "ck_sys_tenant_name_format",
        "sys_tenant",
        "btrim(tenant_name) <> '' AND tenant_name !~ '[[:cntrl:]]'",
    )
    op.create_check_constraint(
        "ck_sys_tenant_lifecycle_status",
        "sys_tenant",
        "(lifecycle_state = 'active' AND status = '1') OR "
        "(lifecycle_state IN ('prepared', 'disabled') AND status = '2')",
    )
    op.create_check_constraint(
        "ck_sys_tenant_provisioning_hashes",
        "sys_tenant",
        "(provisioning_key_hash IS NULL AND provisioning_fingerprint IS NULL) "
        "OR (provisioning_key_hash IS NOT NULL "
        "AND provisioning_fingerprint IS NOT NULL "
        "AND provisioning_key_hash ~ '^[0-9a-f]{64}$' "
        "AND provisioning_fingerprint ~ '^[0-9a-f]{64}$')",
    )
    op.create_unique_constraint(
        "uq_sys_tenant_provisioning_key_hash",
        "sys_tenant",
        ["provisioning_key_hash"],
    )
    op.drop_constraint(
        "sys_platform_audit_log_target_tenant_id_fkey",
        "sys_platform_audit_log",
        type_="foreignkey",
    )
    op.create_check_constraint(
        "ck_platform_audit_target_tenant_id",
        "sys_platform_audit_log",
        "target_tenant_id IS NULL OR target_tenant_id >= 0",
    )
    op.execute(_CREATE_TENANT_VERSION_FUNCTION)
    op.execute(_CREATE_TENANT_VERSION_TRIGGER)


def downgrade() -> None:
    _assert_downgrade_safe()
    op.execute("DROP TRIGGER IF EXISTS trg_sys_tenant_security_version ON sys_tenant")
    op.execute("DROP FUNCTION IF EXISTS bump_sys_tenant_security_version()")
    op.execute(
        "ALTER TABLE sys_platform_audit_log "
        "DROP CONSTRAINT IF EXISTS ck_platform_audit_target_tenant_id"
    )
    # Failed prepare attempts can legitimately reference a target ID with no tenant
    # row. NOT VALID restores enforcement for future writes without erasing history.
    op.execute(
        sa.text(
            "ALTER TABLE sys_platform_audit_log "
            "ADD CONSTRAINT sys_platform_audit_log_target_tenant_id_fkey "
            "FOREIGN KEY (target_tenant_id) REFERENCES sys_tenant(tenant_id) "
            "ON DELETE RESTRICT NOT VALID"
        )
    )
    op.drop_constraint(
        "uq_sys_tenant_provisioning_key_hash",
        "sys_tenant",
        type_="unique",
    )
    op.drop_constraint(
        "ck_sys_tenant_provisioning_hashes",
        "sys_tenant",
        type_="check",
    )
    op.drop_constraint(
        "ck_sys_tenant_lifecycle_status",
        "sys_tenant",
        type_="check",
    )
    op.execute(
        "ALTER TABLE sys_tenant "
        "DROP CONSTRAINT IF EXISTS ck_sys_tenant_name_format, "
        "DROP CONSTRAINT IF EXISTS ck_sys_tenant_code_format, "
        "DROP CONSTRAINT IF EXISTS ck_sys_tenant_nonnegative_id"
    )
    op.drop_column("sys_tenant", "provisioning_fingerprint")
    op.drop_column("sys_tenant", "provisioning_key_hash")
    op.drop_column("sys_tenant", "lifecycle_state")
