"""Drop implicit tenant defaults from contained Marketplace storage.

Revision ID: 1c2d3e4f5a6b
Revises: f0a1b2c3d4e5
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "1c2d3e4f5a6b"
down_revision: str | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DROP_DYNAMIC_TABLE_DEFAULT_SQL = sa.text(
    """
    DO $$
    DECLARE
        target record;
    BEGIN
        FOR target IN
            SELECT table_schema, table_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name LIKE 'app_data\\_%' ESCAPE '\\'
              AND column_name = 'tenant_id'
        LOOP
            EXECUTE format(
                'ALTER TABLE %I.%I ALTER COLUMN tenant_id DROP DEFAULT',
                target.table_schema,
                target.table_name
            );
        END LOOP;
    END $$
    """
)

_RESTORE_DYNAMIC_TABLE_DEFAULT_SQL = sa.text(
    """
    DO $$
    DECLARE
        target record;
    BEGIN
        FOR target IN
            SELECT table_schema, table_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name LIKE 'app_data\\_%' ESCAPE '\\'
              AND column_name = 'tenant_id'
        LOOP
            EXECUTE format(
                'ALTER TABLE %I.%I ALTER COLUMN tenant_id SET DEFAULT 0',
                target.table_schema,
                target.table_name
            );
        END LOOP;
    END $$
    """
)


def _alter_known_tables(*, restore_default: bool) -> None:
    tenant_comments = {
        "mk_app": (
            "租户 ID；单租户模式默认 0；查询必须强制过滤",
            "租户 ID；必须由可信 TenantContext 显式写入",
        ),
        "mk_tenant_app": (
            "租户 ID；单租户模式默认 0",
            "租户 ID；必须由可信 TenantContext 显式写入",
        ),
    }
    for table_name in ("mk_app", "mk_tenant_app"):
        legacy_comment, explicit_comment = tenant_comments[table_name]
        op.alter_column(
            table_name,
            "tenant_id",
            existing_type=sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0") if restore_default else None,
            existing_comment=explicit_comment if restore_default else legacy_comment,
            comment=legacy_comment if restore_default else explicit_comment,
        )


def upgrade() -> None:
    _alter_known_tables(restore_default=False)
    op.execute(_DROP_DYNAMIC_TABLE_DEFAULT_SQL)


def downgrade() -> None:
    _alter_known_tables(restore_default=True)
    op.execute(_RESTORE_DYNAMIC_TABLE_DEFAULT_SQL)
