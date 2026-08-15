"""add immutable AI authorization lineage

Revision ID: c9f5d8e3b2a1
Revises: b8e4c7d2a1f0
Create Date: 2026-08-15 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9f5d8e3b2a1"
down_revision: Union[str, Sequence[str], None] = "b8e4c7d2a1f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ai_message", sa.Column("tenant_id", sa.BigInteger(), nullable=True))
    op.add_column("ai_message", sa.Column("tool_codes", sa.JSON(), nullable=True))
    op.add_column("ai_message", sa.Column("subject_refs", sa.JSON(), nullable=True))
    op.add_column(
        "ai_message",
        sa.Column("subject_refs_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ai_message",
        sa.Column("data_scope_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ai_message",
        sa.Column("resolver_version", sa.String(length=32), nullable=True),
    )

    op.add_column(
        "ai_prepared_action",
        sa.Column("resolved_model_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "ai_prepared_action",
        sa.Column("resolved_provider_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "ai_prepared_action", sa.Column("tool_codes", sa.JSON(), nullable=True)
    )
    op.add_column(
        "ai_prepared_action", sa.Column("subject_refs", sa.JSON(), nullable=True)
    )
    op.add_column(
        "ai_prepared_action",
        sa.Column("subject_refs_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ai_prepared_action",
        sa.Column("data_scope_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ai_prepared_action",
        sa.Column("resolver_version", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_prepared_action", "resolver_version")
    op.drop_column("ai_prepared_action", "data_scope_hash")
    op.drop_column("ai_prepared_action", "subject_refs_hash")
    op.drop_column("ai_prepared_action", "subject_refs")
    op.drop_column("ai_prepared_action", "tool_codes")
    op.drop_column("ai_prepared_action", "resolved_provider_id")
    op.drop_column("ai_prepared_action", "resolved_model_id")
    op.drop_column("ai_message", "resolver_version")
    op.drop_column("ai_message", "data_scope_hash")
    op.drop_column("ai_message", "subject_refs_hash")
    op.drop_column("ai_message", "subject_refs")
    op.drop_column("ai_message", "tool_codes")
    op.drop_column("ai_message", "tenant_id")
