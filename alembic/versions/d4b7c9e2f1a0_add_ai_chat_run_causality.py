"""add AI chat run causality and active projection fields

Revision ID: d4b7c9e2f1a0
Revises: a6f4d2c8e1b9
Create Date: 2026-08-07 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4b7c9e2f1a0"
down_revision: Union[str, Sequence[str], None] = "a6f4d2c8e1b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_message",
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
            comment="当前 active projection；inactive 仅供审计",
        ),
    )
    op.add_column(
        "ai_message",
        sa.Column(
            "supersedes_message_id",
            sa.BigInteger(),
            nullable=True,
            comment="本消息替换的原 message_id；不复用 parent_message_id",
        ),
    )
    op.add_column(
        "ai_operation_log",
        sa.Column(
            "source_user_message_id",
            sa.BigInteger(),
            nullable=True,
            comment="触发 operation 的 user message；NULL 仅兼容历史数据",
        ),
    )
    op.add_column(
        "ai_operation_log",
        sa.Column(
            "readonly_snapshot",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="执行时 AiToolMeta.readonly 快照；未知按 write 处理",
        ),
    )
    op.create_index(
        "ix_ai_message_active_history",
        "ai_message",
        ["conversation_id", "create_time", "message_id"],
        unique=False,
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "uq_ai_message_assistant_run",
        "ai_message",
        ["conversation_id", "trace_id"],
        unique=True,
        postgresql_where=sa.text("role = 'assistant' AND trace_id IS NOT NULL"),
    )
    op.create_index(
        "ix_ai_message_supersedes_message_id",
        "ai_message",
        ["supersedes_message_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_operation_source_status",
        "ai_operation_log",
        ["conversation_id", "source_user_message_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_operation_source_status", table_name="ai_operation_log")
    op.drop_index("ix_ai_message_supersedes_message_id", table_name="ai_message")
    op.drop_index("uq_ai_message_assistant_run", table_name="ai_message")
    op.drop_index("ix_ai_message_active_history", table_name="ai_message")
    op.drop_column("ai_operation_log", "readonly_snapshot")
    op.drop_column("ai_operation_log", "source_user_message_id")
    op.drop_column("ai_message", "supersedes_message_id")
    op.drop_column("ai_message", "is_active")
