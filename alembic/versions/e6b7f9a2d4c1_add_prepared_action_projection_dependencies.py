"""add prepared action projection dependencies

Revision ID: e6b7f9a2d4c1
Revises: d4a6e8f1c3b2
Create Date: 2026-08-15 00:00:01.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e6b7f9a2d4c1"
down_revision: Union[str, Sequence[str], None] = "d4a6e8f1c3b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_prepared_action",
        sa.Column(
            "projection_dependency_message_ids",
            sa.JSON(),
            nullable=True,
            comment="Immutable prior assistant message IDs used as model context",
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_prepared_action", "projection_dependency_message_ids")
