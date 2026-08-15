"""add tenant scope to AI operation log

Revision ID: b8e4c7d2a1f0
Revises: a7d3e9f1c5b2
Create Date: 2026-08-15 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8e4c7d2a1f0"
down_revision: Union[str, Sequence[str], None] = "a7d3e9f1c5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_operation_log",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_index(
        "ix_ai_operation_tenant_trace",
        "ai_operation_log",
        ["tenant_id", "trace_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_operation_tenant_queued_log",
        "ai_operation_log",
        ["tenant_id", "queued_at", "log_id"],
        unique=False,
    )
    # 历史单租户行已由临时 default 回填；新写入必须由可信上下文显式提供。
    op.alter_column("ai_operation_log", "tenant_id", server_default=None)


def downgrade() -> None:
    op.drop_index(
        "ix_ai_operation_tenant_queued_log",
        table_name="ai_operation_log",
    )
    op.drop_index("ix_ai_operation_tenant_trace", table_name="ai_operation_log")
    op.drop_column("ai_operation_log", "tenant_id")
