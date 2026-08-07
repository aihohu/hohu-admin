"""add sys_file owner and tenant anchors

Revision ID: a6f4d2c8e1b9
Revises: 00558ec10892
Create Date: 2026-08-07 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a6f4d2c8e1b9"
down_revision: Union[str, Sequence[str], None] = "00558ec10892"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add immutable owner and trusted tenant anchors to uploaded files."""
    op.add_column(
        "sys_file",
        sa.Column(
            "owner_user_id",
            sa.BigInteger(),
            nullable=True,
            comment="文件所有者用户ID（NULL 仅兼容无法回填的历史记录）",
        ),
    )
    op.add_column(
        "sys_file",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
            comment="租户ID（当前单租户固定为0）",
        ),
    )

    # Do not backfill from create_by/user_name.  user_name is mutable and users
    # are hard-deleted, so a later account may reuse the same name.  Assigning
    # those legacy files would create an IDOR.  NULL is intentionally fail-closed;
    # only uploads made after this migration receive an authenticated owner ID.


def downgrade() -> None:
    """Remove uploaded-file security anchors."""
    op.drop_column("sys_file", "tenant_id")
    op.drop_column("sys_file", "owner_user_id")
