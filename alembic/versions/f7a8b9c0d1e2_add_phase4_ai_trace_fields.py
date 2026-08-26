"""add Phase 4 AI Trace immutable audit fields

Revision ID: f7a8b9c0d1e2
Revises: e6b7f9a2d4c1
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = "e6b7f9a2d4c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_operation_log",
        sa.Column("agent_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ai_operation_log",
        sa.Column("target_summary", sa.Text(), nullable=True),
    )
    op.execute(
        """
        UPDATE ai_operation_log AS operation
        SET agent_code = action.agent_code
        FROM ai_prepared_action AS action
        WHERE action.execute_tool_call_id = operation.tool_call_id
          AND operation.agent_code IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("ai_operation_log", "target_summary")
    op.drop_column("ai_operation_log", "agent_code")
