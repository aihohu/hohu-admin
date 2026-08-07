"""add durable AI prepared actions

Revision ID: e8a1f4c2d7b6
Revises: d4b7c9e2f1a0
Create Date: 2026-08-07 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8a1f4c2d7b6"
down_revision: Union[str, Sequence[str], None] = "d4b7c9e2f1a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_prepared_action",
        sa.Column("action_id", sa.BigInteger(), nullable=False),
        sa.Column("confirmation_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending_confirmation'"),
            nullable=False,
        ),
        sa.Column(
            "row_version", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("interaction_flow", sa.String(length=32), nullable=False),
        sa.Column("requested_outcome", sa.String(length=32), nullable=False),
        sa.Column("approval_mode", sa.String(length=32), nullable=False),
        sa.Column("dispatch_mode", sa.String(length=32), nullable=False),
        sa.Column("prepare_tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("execute_tool_call_id", sa.String(length=64), nullable=False),
        sa.Column("execute_tool_name", sa.String(length=128), nullable=False),
        sa.Column("frozen_args", sa.JSON(), nullable=False),
        sa.Column("args_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("subject_ref", sa.JSON(), nullable=True),
        sa.Column("presentation", sa.JSON(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_user_message_id", sa.BigInteger(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("agent_code", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'pending_confirmation', 'approved', "
            "'running', 'succeeded', 'failed', 'rejected', 'expired')",
            name="ck_ai_prepared_action_status",
        ),
        sa.PrimaryKeyConstraint("action_id"),
        sa.UniqueConstraint(
            "confirmation_id", name="uq_ai_prepared_action_confirmation_id"
        ),
        sa.UniqueConstraint(
            "execute_tool_call_id", name="uq_ai_prepared_action_execute_tool_call_id"
        ),
    )
    op.create_index(
        "ix_ai_prepared_action_conversation_status_expires",
        "ai_prepared_action",
        ["conversation_id", "status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_prepared_action_source_status",
        "ai_prepared_action",
        ["source_user_message_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_prepared_action_source_status", table_name="ai_prepared_action"
    )
    op.drop_index(
        "ix_ai_prepared_action_conversation_status_expires",
        table_name="ai_prepared_action",
    )
    op.drop_table("ai_prepared_action")
