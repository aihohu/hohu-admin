"""Reconcile release schema parity for governed AI metadata.

Revision ID: 5a6b7c8d9e0f
Revises: 4f5a6b7c8d9e
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "5a6b7c8d9e0f"
down_revision: str | None = "4f5a6b7c8d9e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _assert_ai_model_timestamps_present() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE ai_model IN ACCESS EXCLUSIVE MODE"))
    has_null = connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM ai_model "
            "WHERE create_time IS NULL OR update_time IS NULL)"
        )
    ).scalar_one()
    if has_null:
        raise RuntimeError(
            "PLAN6_AI_MODEL_TIMESTAMP_NULL: repair ai_model timestamps before upgrade"
        )


def upgrade() -> None:
    """Make reflected database metadata match the release ORM contract."""
    _assert_ai_model_timestamps_present()

    op.alter_column(
        "ai_agent",
        "enabled",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        comment="全局开关；ORM 默认禁用，fresh seed 按阶段发布集合决定",
        existing_comment="全局开关，默认禁用，部署方按需启用",
    )
    op.alter_column(
        "ai_conversation",
        "deleted_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        comment="Soft deletion timestamp; NULL means active",
        existing_comment=None,
    )
    op.alter_column(
        "ai_message",
        "tool_calls",
        existing_type=sa.JSON(),
        existing_nullable=True,
        comment="工具调用记录列表（名称、参数、结果）",
        existing_comment="工具调用记录（名称、参数、结果）",
    )
    op.alter_column(
        "ai_model",
        "model_id",
        existing_type=sa.BigInteger(),
        existing_nullable=False,
        comment="模型ID",
        existing_comment=None,
    )
    op.alter_column(
        "ai_model",
        "is_enabled",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        comment="是否启用",
        existing_comment=None,
    )
    op.alter_column(
        "ai_model",
        "sort_order",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment="排序（越小越靠前）",
        existing_comment=None,
    )
    for column_name, comment in (
        ("create_time", "创建时间"),
        ("update_time", "更新时间"),
    ):
        op.alter_column(
            "ai_model",
            column_name,
            existing_type=sa.DateTime(),
            existing_nullable=True,
            nullable=False,
            existing_server_default=sa.text("now()"),
            comment=comment,
            existing_comment=None,
        )
    for column_name, column_type, comment in (
        (
            "agent_code",
            sa.String(length=64),
            "Immutable executing Agent code; NULL means unknown legacy data",
        ),
        (
            "target_summary",
            sa.Text(),
            "Allowlisted immutable target references for audit display",
        ),
        (
            "result_summary",
            sa.Text(),
            "status + affected_count + duration_ms + error_code",
        ),
    ):
        op.alter_column(
            "ai_operation_log",
            column_name,
            existing_type=column_type,
            existing_nullable=True,
            comment=comment,
            existing_comment=None,
        )
    op.alter_column(
        "ai_prepared_action",
        "action_id",
        existing_type=sa.BigInteger(),
        existing_nullable=False,
        comment="Snowflake action ID",
        existing_comment=None,
    )


def downgrade() -> None:
    """Restore the metadata contract from revision 4f5a6b7c8d9e."""
    op.alter_column(
        "ai_prepared_action",
        "action_id",
        existing_type=sa.BigInteger(),
        existing_nullable=False,
        comment=None,
        existing_comment="Snowflake action ID",
    )
    for column_name, column_type, existing_comment in (
        (
            "agent_code",
            sa.String(length=64),
            "Immutable executing Agent code; NULL means unknown legacy data",
        ),
        (
            "target_summary",
            sa.Text(),
            "Allowlisted immutable target references for audit display",
        ),
        (
            "result_summary",
            sa.Text(),
            "status + affected_count + duration_ms + error_code",
        ),
    ):
        op.alter_column(
            "ai_operation_log",
            column_name,
            existing_type=column_type,
            existing_nullable=True,
            comment=None,
            existing_comment=existing_comment,
        )
    for column_name, existing_comment in (
        ("update_time", "更新时间"),
        ("create_time", "创建时间"),
    ):
        op.alter_column(
            "ai_model",
            column_name,
            existing_type=sa.DateTime(),
            existing_nullable=False,
            nullable=True,
            existing_server_default=sa.text("now()"),
            comment=None,
            existing_comment=existing_comment,
        )
    for column_name, column_type, existing_comment in (
        ("sort_order", sa.Integer(), "排序（越小越靠前）"),
        ("is_enabled", sa.Boolean(), "是否启用"),
        ("model_id", sa.BigInteger(), "模型ID"),
    ):
        op.alter_column(
            "ai_model",
            column_name,
            existing_type=column_type,
            existing_nullable=False,
            comment=None,
            existing_comment=existing_comment,
        )
    op.alter_column(
        "ai_message",
        "tool_calls",
        existing_type=sa.JSON(),
        existing_nullable=True,
        comment="工具调用记录（名称、参数、结果）",
        existing_comment="工具调用记录列表（名称、参数、结果）",
    )
    op.alter_column(
        "ai_conversation",
        "deleted_at",
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
        comment=None,
        existing_comment="Soft deletion timestamp; NULL means active",
    )
    op.alter_column(
        "ai_agent",
        "enabled",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        comment="全局开关，默认禁用，部署方按需启用",
        existing_comment="全局开关；ORM 默认禁用，fresh seed 按阶段发布集合决定",
    )
