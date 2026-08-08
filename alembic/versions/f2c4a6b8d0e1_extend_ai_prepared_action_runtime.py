"""extend durable AI prepared action runtime state

Revision ID: f2c4a6b8d0e1
Revises: e8a1f4c2d7b6
Create Date: 2026-08-08 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2c4a6b8d0e1"
down_revision: Union[str, Sequence[str], None] = "e8a1f4c2d7b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_prepared_action",
        sa.Column("guard_owner_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ai_prepared_action",
        sa.Column(
            "command_action",
            sa.String(length=16),
            server_default=sa.text("'send'"),
            nullable=False,
        ),
    )
    op.add_column(
        "ai_prepared_action",
        sa.Column(
            "risk_level",
            sa.String(length=16),
            server_default=sa.text("'high'"),
            nullable=False,
        ),
    )
    op.add_column(
        "ai_prepared_action",
        sa.Column("chip_target", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "ai_prepared_action", sa.Column("result_data", sa.JSON(), nullable=True)
    )
    op.add_column(
        "ai_prepared_action", sa.Column("result_ui", sa.JSON(), nullable=True)
    )
    op.add_column(
        "ai_prepared_action", sa.Column("duration_ms", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("ai_prepared_action", "duration_ms")
    op.drop_column("ai_prepared_action", "result_ui")
    op.drop_column("ai_prepared_action", "result_data")
    op.drop_column("ai_prepared_action", "chip_target")
    op.drop_column("ai_prepared_action", "risk_level")
    op.drop_column("ai_prepared_action", "command_action")
    op.drop_column("ai_prepared_action", "guard_owner_token")
