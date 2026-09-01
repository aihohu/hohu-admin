"""Add the M1 tenant principal foundation.

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: str | Sequence[str] | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the registry, seed Default Tenant, and shadow-scope users."""
    op.create_table(
        "sys_tenant",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column(
            "tenant_code",
            sa.String(length=32),
            nullable=False,
            comment="稳定的小写租户代码",
        ),
        sa.Column(
            "tenant_name",
            sa.String(length=100),
            nullable=False,
            comment="租户展示名称",
        ),
        sa.Column(
            "status",
            sa.String(length=2),
            nullable=False,
            server_default="1",
            comment="状态",
        ),
        sa.Column(
            "row_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="授权与缓存漂移检测版本",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("status IN ('1', '2')", name="ck_sys_tenant_status"),
        sa.CheckConstraint("row_version >= 1", name="ck_sys_tenant_row_version"),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.UniqueConstraint("tenant_code", name="uq_sys_tenant_tenant_code"),
        comment="平台全局租户注册表",
    )

    tenant_table = sa.table(
        "sys_tenant",
        sa.column("tenant_id", sa.BigInteger()),
        sa.column("tenant_code", sa.String()),
        sa.column("tenant_name", sa.String()),
        sa.column("status", sa.String()),
        sa.column("row_version", sa.Integer()),
    )
    op.bulk_insert(
        tenant_table,
        [
            {
                "tenant_id": 0,
                "tenant_code": "default",
                "tenant_name": "Default Tenant",
                "status": "1",
                "row_version": 1,
            }
        ],
    )

    op.add_column(
        "sys_user",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=True,
            comment="租户ID；M1 兼容期由服务端 Default Tenant 回填",
        ),
    )
    op.execute("UPDATE sys_user SET tenant_id = 0 WHERE tenant_id IS NULL")
    op.alter_column(
        "sys_user",
        "tenant_id",
        existing_type=sa.BigInteger(),
        nullable=False,
        server_default=sa.text("0"),
        existing_comment="租户ID；M1 兼容期由服务端 Default Tenant 回填",
    )
    op.create_foreign_key(
        "fk_sys_user_tenant_id_sys_tenant",
        "sys_user",
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_sys_user_tenant_id", "sys_user", ["tenant_id"], unique=False)
    op.create_index(
        "ix_sys_user_tenant_user_name",
        "sys_user",
        ["tenant_id", "user_name"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the M1 shadow scope and tenant registry."""
    op.drop_index("ix_sys_user_tenant_user_name", table_name="sys_user")
    op.drop_index("ix_sys_user_tenant_id", table_name="sys_user")
    op.drop_constraint(
        "fk_sys_user_tenant_id_sys_tenant", "sys_user", type_="foreignkey"
    )
    op.drop_column("sys_user", "tenant_id")
    op.drop_table("sys_tenant")
