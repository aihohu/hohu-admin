"""Add the atomic prepared-tenant bootstrap marker.

Revision ID: 4f5a6b7c8d9e
Revises: 3e4f5a6b7c8d
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4f5a6b7c8d9e"
down_revision: str | None = "3e4f5a6b7c8d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CREATE_TENANT_VERSION_FUNCTION = sa.text(
    """
    CREATE OR REPLACE FUNCTION bump_sys_tenant_security_version()
    RETURNS trigger AS $$
    BEGIN
        IF NEW.tenant_code IS DISTINCT FROM OLD.tenant_code THEN
            RAISE EXCEPTION 'tenant_code is immutable'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status
           OR NEW.lifecycle_state IS DISTINCT FROM OLD.lifecycle_state
           OR NEW.bootstrap_version IS DISTINCT FROM OLD.bootstrap_version THEN
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

_RESTORE_TENANT_VERSION_FUNCTION = sa.text(
    """
    CREATE OR REPLACE FUNCTION bump_sys_tenant_security_version()
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


def _assert_upgrade_safe() -> None:
    legacy_active = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM sys_tenant "
                "WHERE tenant_id <> 0 AND lifecycle_state = 'active')"
            )
        )
        .scalar_one()
    )
    if legacy_active:
        raise RuntimeError("PLAN5BB_LEGACY_ACTIVE_TENANT_REQUIRES_REVIEW")


def _assert_downgrade_safe() -> None:
    bootstrapped = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM sys_tenant "
                "WHERE tenant_id <> 0 AND bootstrap_version > 0)"
            )
        )
        .scalar_one()
    )
    if bootstrapped:
        raise RuntimeError("PLAN5BB_DOWNGRADE_BOOTSTRAPPED_TENANT")


def upgrade() -> None:
    _assert_upgrade_safe()
    op.add_column(
        "sys_tenant",
        sa.Column(
            "bootstrap_version",
            sa.Integer(),
            nullable=True,
            comment="租户原子引导版本；0=未引导，1=Plan 5-B-B 完成",
        ),
    )
    op.add_column(
        "sys_tenant",
        sa.Column(
            "bootstrap_key_hash",
            sa.String(length=64),
            nullable=True,
            comment="tenant bootstrap 幂等键 SHA-256",
        ),
    )
    op.add_column(
        "sys_tenant",
        sa.Column(
            "bootstrap_fingerprint",
            sa.String(length=64),
            nullable=True,
            comment="tenant bootstrap 请求 keyed-HMAC fingerprint",
        ),
    )
    op.execute(
        sa.text(
            "UPDATE sys_tenant SET bootstrap_version = "
            "CASE WHEN tenant_id = 0 THEN 1 ELSE 0 END"
        )
    )
    op.alter_column(
        "sys_tenant", "bootstrap_version", nullable=False, server_default="0"
    )
    op.create_check_constraint(
        "ck_sys_tenant_bootstrap_state",
        "sys_tenant",
        "(tenant_id = 0 AND bootstrap_version = 1 "
        "AND bootstrap_key_hash IS NULL AND bootstrap_fingerprint IS NULL) OR "
        "(tenant_id <> 0 AND bootstrap_version = 0 "
        "AND bootstrap_key_hash IS NULL AND bootstrap_fingerprint IS NULL) OR "
        "(tenant_id <> 0 AND bootstrap_version = 1 "
        "AND bootstrap_key_hash IS NOT NULL "
        "AND bootstrap_fingerprint IS NOT NULL "
        "AND bootstrap_key_hash ~ '^[0-9a-f]{64}$' "
        "AND bootstrap_fingerprint ~ '^[0-9a-f]{64}$')",
    )
    op.create_check_constraint(
        "ck_sys_tenant_active_bootstrapped",
        "sys_tenant",
        "lifecycle_state <> 'active' OR bootstrap_version >= 1",
    )
    op.create_unique_constraint(
        "uq_sys_tenant_bootstrap_key_hash",
        "sys_tenant",
        ["bootstrap_key_hash"],
    )
    op.execute(_CREATE_TENANT_VERSION_FUNCTION)


def downgrade() -> None:
    _assert_downgrade_safe()
    op.execute(_RESTORE_TENANT_VERSION_FUNCTION)
    op.drop_constraint("uq_sys_tenant_bootstrap_key_hash", "sys_tenant", type_="unique")
    op.drop_constraint("ck_sys_tenant_active_bootstrapped", "sys_tenant", type_="check")
    op.drop_constraint("ck_sys_tenant_bootstrap_state", "sys_tenant", type_="check")
    op.drop_column("sys_tenant", "bootstrap_fingerprint")
    op.drop_column("sys_tenant", "bootstrap_key_hash")
    op.drop_column("sys_tenant", "bootstrap_version")
