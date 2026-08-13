"""add prepared action execution lease

Revision ID: a7d3e9f1c5b2
Revises: f2c4a6b8d0e1
Create Date: 2026-08-13 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3e9f1c5b2"
down_revision: Union[str, Sequence[str], None] = "f2c4a6b8d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_prepared_action",
        sa.Column("execution_owner", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ai_prepared_action",
        sa.Column(
            "execution_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_prepared_action", "execution_lease_expires_at")
    op.drop_column("ai_prepared_action", "execution_owner")
