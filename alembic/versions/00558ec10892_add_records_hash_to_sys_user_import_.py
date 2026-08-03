"""add records_hash to sys_user_import_batch

Revision ID: 00558ec10892
Revises: 0b2165376771
Create Date: 2026-08-03 15:49:33.325253

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "00558ec10892"
down_revision: Union[str, Sequence[str], None] = "0b2165376771"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """加 records_hash 列（spec §2.19 三重校验字段）。

    batch 行由 dry_run 创建（status=CREATED）→ records_hash 在 INSERT 时即写入，
    所以 nullable=False 不需要 backfill。
    """
    op.add_column(
        "sys_user_import_batch",
        sa.Column(
            "records_hash",
            sa.String(length=64),
            nullable=False,
            server_default="",
            comment="records 序列化后的 sha256（spec §2.19 execute 三重校验）",
        ),
    )
    # server_default="" 仅为加列时通过 NOT NULL 约束；后续 INSERT 永远显式提供真实 hash
    op.alter_column(
        "sys_user_import_batch",
        "records_hash",
        server_default=None,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("sys_user_import_batch", "records_hash")
