"""add AI message projection dependencies

Revision ID: d4a6e8f1c3b2
Revises: c9f5d8e3b2a1
Create Date: 2026-08-15 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4a6e8f1c3b2"
down_revision: Union[str, Sequence[str], None] = "c9f5d8e3b2a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_message",
        sa.Column(
            "projection_dependency_message_ids",
            sa.JSON(),
            nullable=True,
            comment="Immutable prior assistant message IDs used as model context",
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_message", "projection_dependency_message_ids")
