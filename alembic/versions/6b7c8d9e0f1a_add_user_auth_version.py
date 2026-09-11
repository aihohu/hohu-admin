"""Add per-user authentication version.

Revision ID: 6b7c8d9e0f1a
Revises: 5a6b7c8d9e0f
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "6b7c8d9e0f1a"
down_revision: str | None = "5a6b7c8d9e0f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Backfill legacy users and enforce a positive authentication version."""
    op.add_column(
        "sys_user",
        sa.Column(
            "auth_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
            comment="认证版本；安全敏感变更后递增以撤销既有会话",
        ),
    )
    op.create_check_constraint(
        "ck_sys_user_auth_version_positive",
        "sys_user",
        "auth_version >= 1",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_sys_user_auth_version_positive",
        "sys_user",
        type_="check",
    )
    op.drop_column("sys_user", "auth_version")
