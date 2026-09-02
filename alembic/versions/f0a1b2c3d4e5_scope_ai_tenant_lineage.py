"""Scope AI user facts and model eligibility by tenant.

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql import Executable

revision: str = "f0a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _scalar_count(statement: str | Executable) -> int:
    query = sa.text(statement) if isinstance(statement, str) else statement
    return int(op.get_bind().execute(query).scalar_one())


def _require_zero(statement: str | Executable, *, error_code: str) -> None:
    count = _scalar_count(statement)
    if count:
        raise RuntimeError(f"{error_code}: {count} row(s)")


def _add_tenant_column(table_name: str, *, comment: str) -> None:
    op.add_column(
        table_name,
        sa.Column("tenant_id", sa.BigInteger(), nullable=True, comment=comment),
    )


def _backfill_unresolved_routing_audit() -> None:
    """Use the single-tenant fallback only while no second tenant exists."""
    unresolved = _scalar_count(
        """
        SELECT
            (SELECT count(*) FROM ai_routing_log WHERE tenant_id IS NULL)
          + (SELECT count(*) FROM ai_routing_feedback WHERE tenant_id IS NULL)
        """
    )
    if not unresolved:
        return
    _require_zero(
        "SELECT count(*) FROM sys_tenant WHERE tenant_id <> 0",
        error_code="TENANT_BACKFILL_AMBIGUOUS_ROUTING_AUDIT",
    )
    op.execute(
        sa.text(
            """
            UPDATE ai_routing_log
            SET tenant_id = 0
            WHERE tenant_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ai_routing_feedback
            SET tenant_id = 0
            WHERE tenant_id IS NULL
            """
        )
    )


def _backfill_tenant_lineage() -> None:
    _add_tenant_column(
        "ai_conversation",
        comment="租户ID；必须由可信 TenantContext 显式写入",
    )
    _add_tenant_column(
        "ai_routing_log",
        comment="租户ID；必须由可信 TenantContext 显式写入",
    )
    _add_tenant_column(
        "ai_routing_feedback",
        comment="租户ID；必须与反馈消息一致",
    )

    op.execute(
        sa.text(
            """
            UPDATE ai_conversation AS conversation
            SET tenant_id = actor.tenant_id
            FROM sys_user AS actor
            WHERE actor.user_id = conversation.user_id
              AND conversation.tenant_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ai_message AS message
            SET tenant_id = conversation.tenant_id
            FROM ai_conversation AS conversation
            WHERE conversation.conversation_id = message.conversation_id
              AND message.tenant_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ai_routing_log AS routing
            SET tenant_id = conversation.tenant_id
            FROM ai_conversation AS conversation
            WHERE conversation.conversation_id = routing.conversation_id
              AND routing.tenant_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ai_routing_log AS routing
            SET tenant_id = actor.tenant_id
            FROM sys_user AS actor
            WHERE actor.user_id = routing.user_id
              AND routing.tenant_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ai_routing_feedback AS feedback
            SET tenant_id = message.tenant_id
            FROM ai_message AS message
            WHERE message.message_id = feedback.message_id
              AND feedback.tenant_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ai_routing_feedback AS feedback
            SET tenant_id = actor.tenant_id
            FROM sys_user AS actor
            WHERE actor.user_id = feedback.user_id
              AND feedback.tenant_id IS NULL
            """
        )
    )
    # Routing audit rows intentionally outlive deleted actors/messages.  The
    # fallback is valid only when the database still contains Default Tenant 0
    # exclusively; otherwise an unresolved row has no provable tenant lineage.
    _backfill_unresolved_routing_audit()

    for table_name in (
        "ai_conversation",
        "ai_message",
        "ai_routing_log",
        "ai_routing_feedback",
        "ai_prepared_action",
        "ai_operation_log",
    ):
        table = sa.table(table_name, sa.column("tenant_id"))
        _require_zero(
            sa.select(sa.func.count())
            .select_from(table)
            .where(table.c.tenant_id.is_(None)),
            error_code=f"TENANT_BACKFILL_NULL:{table_name}",
        )

    checks = {
        "TENANT_CROSS_LINK:ai_message.conversation": """
            SELECT count(*) FROM ai_message message
            LEFT JOIN ai_conversation conversation
              ON conversation.conversation_id = message.conversation_id
            WHERE conversation.conversation_id IS NULL
               OR message.tenant_id <> conversation.tenant_id
        """,
        "TENANT_CROSS_LINK:ai_routing_log.user": """
            SELECT count(*) FROM ai_routing_log routing
            LEFT JOIN sys_user actor ON actor.user_id = routing.user_id
            WHERE actor.user_id IS NOT NULL
              AND routing.tenant_id <> actor.tenant_id
        """,
        "TENANT_CROSS_LINK:ai_routing_log.conversation": """
            SELECT count(*) FROM ai_routing_log routing
            JOIN ai_conversation conversation
              ON conversation.conversation_id = routing.conversation_id
            WHERE routing.conversation_id IS NOT NULL
              AND routing.tenant_id <> conversation.tenant_id
        """,
        "TENANT_CROSS_LINK:ai_routing_feedback.message": """
            SELECT count(*) FROM ai_routing_feedback feedback
            LEFT JOIN ai_message message ON message.message_id = feedback.message_id
            WHERE message.message_id IS NOT NULL
              AND feedback.tenant_id <> message.tenant_id
        """,
        "TENANT_CROSS_LINK:ai_routing_feedback.user": """
            SELECT count(*) FROM ai_routing_feedback feedback
            LEFT JOIN sys_user actor ON actor.user_id = feedback.user_id
            WHERE actor.user_id IS NOT NULL
              AND feedback.tenant_id <> actor.tenant_id
        """,
        "TENANT_CROSS_LINK:ai_prepared_action": """
            SELECT count(*) FROM ai_prepared_action action
            LEFT JOIN ai_conversation conversation
              ON conversation.conversation_id = action.conversation_id
            LEFT JOIN ai_message source
              ON source.message_id = action.source_user_message_id
            LEFT JOIN sys_user actor ON actor.user_id = action.user_id
            WHERE conversation.conversation_id IS NULL
               OR source.message_id IS NULL
               OR action.tenant_id <> conversation.tenant_id
               OR action.tenant_id <> source.tenant_id
               OR (
                    actor.user_id IS NOT NULL
                    AND action.tenant_id <> actor.tenant_id
               )
        """,
        "TENANT_CROSS_LINK:ai_operation_log": """
            SELECT count(*) FROM ai_operation_log operation
            LEFT JOIN ai_conversation conversation
              ON conversation.conversation_id = operation.conversation_id
            LEFT JOIN ai_message source
              ON source.message_id = operation.source_user_message_id
            LEFT JOIN sys_user actor ON actor.user_id = operation.user_id
            WHERE conversation.conversation_id IS NULL
               OR operation.tenant_id <> conversation.tenant_id
               OR (
                    actor.user_id IS NOT NULL
                    AND operation.tenant_id <> actor.tenant_id
               )
               OR (
                    operation.source_user_message_id IS NOT NULL
                    AND (
                        source.message_id IS NULL
                        OR operation.tenant_id <> source.tenant_id
                    )
               )
        """,
    }
    for error_code, statement in checks.items():
        _require_zero(statement, error_code=error_code)


def _replace_indexes_and_constraints() -> None:
    op.drop_constraint(
        "ai_conversation_user_id_fkey", "ai_conversation", type_="foreignkey"
    )
    op.drop_constraint(
        "ai_message_conversation_id_fkey", "ai_message", type_="foreignkey"
    )
    op.create_unique_constraint(
        "uq_ai_conversation_tenant_conversation_id",
        "ai_conversation",
        ["tenant_id", "conversation_id"],
    )
    op.create_unique_constraint(
        "uq_ai_message_tenant_message_id",
        "ai_message",
        ["tenant_id", "message_id"],
    )
    op.create_foreign_key(
        "fk_ai_conversation_tenant_user",
        "ai_conversation",
        "sys_user",
        ["tenant_id", "user_id"],
        ["tenant_id", "user_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_ai_message_tenant_conversation",
        "ai_message",
        "ai_conversation",
        ["tenant_id", "conversation_id"],
        ["tenant_id", "conversation_id"],
        ondelete="CASCADE",
    )
    for constraint_name, table_name in (
        ("uq_ai_prepared_action_confirmation_id", "ai_prepared_action"),
        ("uq_ai_prepared_action_execute_tool_call_id", "ai_prepared_action"),
        ("uq_ai_op_log_tool_call_id", "ai_operation_log"),
    ):
        op.drop_constraint(constraint_name, table_name, type_="unique")
    op.create_unique_constraint(
        "uq_ai_prepared_action_tenant_confirmation_id",
        "ai_prepared_action",
        ["tenant_id", "confirmation_id"],
    )
    op.create_unique_constraint(
        "uq_ai_prepared_action_tenant_execute_tool_call_id",
        "ai_prepared_action",
        ["tenant_id", "execute_tool_call_id"],
    )
    op.create_unique_constraint(
        "uq_ai_operation_log_tenant_tool_call_id",
        "ai_operation_log",
        ["tenant_id", "tool_call_id"],
    )

    for index_name, _table_name in (
        ("idx_ai_message_conv_trace", "ai_message"),
        ("ix_ai_message_active_history", "ai_message"),
        ("uq_ai_message_assistant_run", "ai_message"),
        ("ix_ai_message_supersedes_message_id", "ai_message"),
        (
            "ix_ai_prepared_action_conversation_status_expires",
            "ai_prepared_action",
        ),
        ("ix_ai_prepared_action_source_status", "ai_prepared_action"),
        ("ix_ai_operation_source_status", "ai_operation_log"),
        ("ix_ai_operation_tenant_trace", "ai_operation_log"),
        ("ix_ai_routing_log_create_time", "ai_routing_log"),
        ("ix_ai_routing_log_trace_id", "ai_routing_log"),
        ("ix_ai_routing_log_user_id", "ai_routing_log"),
        ("ix_ai_routing_feedback_create_time", "ai_routing_feedback"),
        ("ix_ai_routing_feedback_message_id", "ai_routing_feedback"),
        ("ix_ai_routing_feedback_trace_id", "ai_routing_feedback"),
        ("ix_ai_routing_feedback_user_id", "ai_routing_feedback"),
    ):
        op.execute(sa.text(f'DROP INDEX IF EXISTS "{index_name}"'))

    op.create_index(
        "ix_ai_conversation_tenant_user_updated",
        "ai_conversation",
        ["tenant_id", "user_id", "update_time", "conversation_id"],
    )
    op.create_index(
        "ix_ai_message_active_history",
        "ai_message",
        ["tenant_id", "conversation_id", "create_time", "message_id"],
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "uq_ai_message_assistant_run",
        "ai_message",
        ["tenant_id", "conversation_id", "trace_id"],
        unique=True,
        postgresql_where=sa.text("role = 'assistant' AND trace_id IS NOT NULL"),
    )
    op.create_index(
        "ix_ai_message_tenant_supersedes",
        "ai_message",
        ["tenant_id", "supersedes_message_id"],
    )
    op.create_index(
        "ix_ai_prepared_action_tenant_conversation_status_expires",
        "ai_prepared_action",
        ["tenant_id", "conversation_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_ai_prepared_action_tenant_source_status",
        "ai_prepared_action",
        ["tenant_id", "source_user_message_id", "status"],
    )
    op.create_index(
        "ix_ai_operation_tenant_source_status",
        "ai_operation_log",
        ["tenant_id", "conversation_id", "source_user_message_id", "status"],
    )
    op.create_index(
        "ix_ai_operation_tenant_trace",
        "ai_operation_log",
        ["tenant_id", "trace_id"],
    )
    op.create_index(
        "ix_ai_routing_log_tenant_trace",
        "ai_routing_log",
        ["tenant_id", "trace_id"],
    )
    op.create_index(
        "ix_ai_routing_log_tenant_user_created",
        "ai_routing_log",
        ["tenant_id", "user_id", "create_time"],
    )
    op.create_index(
        "ix_ai_routing_feedback_tenant_message_created",
        "ai_routing_feedback",
        ["tenant_id", "message_id", "create_time"],
    )
    op.create_index(
        "ix_ai_routing_feedback_tenant_trace",
        "ai_routing_feedback",
        ["tenant_id", "trace_id"],
    )


def _create_model_policy() -> None:
    op.create_table(
        "tenant_ai_model_policy",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="获授权租户ID"),
        sa.Column(
            "model_id", sa.BigInteger(), nullable=False, comment="平台全局模型ID"
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="租户是否可使用该模型",
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="租户默认聊天模型",
        ),
        sa.Column(
            "daily_quota_per_user",
            sa.Integer(),
            nullable=True,
            comment="该租户内单用户日配额；NULL 表示使用上层配额",
        ),
        sa.CheckConstraint(
            "daily_quota_per_user IS NULL OR daily_quota_per_user > 0",
            name="ck_tenant_ai_model_policy_positive_quota",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"], ["ai_model.model_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["sys_tenant.tenant_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("tenant_id", "model_id"),
    )
    op.create_index(
        "uq_tenant_ai_model_policy_enabled_default",
        "tenant_ai_model_policy",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("enabled = true AND is_default = true"),
    )
    op.create_index(
        "ix_tenant_ai_model_policy_tenant_enabled_model",
        "tenant_ai_model_policy",
        ["tenant_id", "enabled", "model_id"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO tenant_ai_model_policy (
                tenant_id, model_id, enabled, is_default
            )
            SELECT 0,
                   model_id,
                   true,
                   row_number() OVER (ORDER BY sort_order, model_id) = 1
            FROM ai_model
            WHERE is_enabled = true
            """
        )
    )


def upgrade() -> None:
    _backfill_tenant_lineage()
    for table_name, comment in (
        (
            "ai_conversation",
            "租户ID；必须由可信 TenantContext 显式写入",
        ),
        ("ai_message", "租户ID；必须与所属会话一致"),
        (
            "ai_routing_log",
            "租户ID；必须由可信 TenantContext 显式写入",
        ),
        ("ai_routing_feedback", "租户ID；必须与反馈消息一致"),
        ("ai_operation_log", "可信租户ID；历史单租户数据回填 0"),
    ):
        op.alter_column(
            table_name,
            "tenant_id",
            existing_type=sa.BigInteger(),
            nullable=False,
            comment=comment,
        )
    _replace_indexes_and_constraints()
    _create_model_policy()


def downgrade() -> None:
    op.drop_table("tenant_ai_model_policy")

    for index_name, table_name in (
        ("ix_ai_routing_feedback_tenant_trace", "ai_routing_feedback"),
        (
            "ix_ai_routing_feedback_tenant_message_created",
            "ai_routing_feedback",
        ),
        ("ix_ai_routing_log_tenant_user_created", "ai_routing_log"),
        ("ix_ai_routing_log_tenant_trace", "ai_routing_log"),
        ("ix_ai_operation_tenant_trace", "ai_operation_log"),
        ("ix_ai_operation_tenant_source_status", "ai_operation_log"),
        ("ix_ai_prepared_action_tenant_source_status", "ai_prepared_action"),
        (
            "ix_ai_prepared_action_tenant_conversation_status_expires",
            "ai_prepared_action",
        ),
        ("ix_ai_message_tenant_supersedes", "ai_message"),
        ("uq_ai_message_assistant_run", "ai_message"),
        ("ix_ai_message_active_history", "ai_message"),
        ("ix_ai_conversation_tenant_user_updated", "ai_conversation"),
    ):
        op.drop_index(index_name, table_name=table_name)

    op.drop_constraint(
        "fk_ai_message_tenant_conversation", "ai_message", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_ai_conversation_tenant_user",
        "ai_conversation",
        type_="foreignkey",
    )
    op.drop_constraint("uq_ai_message_tenant_message_id", "ai_message", type_="unique")
    op.drop_constraint(
        "uq_ai_conversation_tenant_conversation_id",
        "ai_conversation",
        type_="unique",
    )
    op.drop_constraint(
        "uq_ai_operation_log_tenant_tool_call_id",
        "ai_operation_log",
        type_="unique",
    )
    op.drop_constraint(
        "uq_ai_prepared_action_tenant_execute_tool_call_id",
        "ai_prepared_action",
        type_="unique",
    )
    op.drop_constraint(
        "uq_ai_prepared_action_tenant_confirmation_id",
        "ai_prepared_action",
        type_="unique",
    )

    op.create_unique_constraint(
        "uq_ai_prepared_action_confirmation_id",
        "ai_prepared_action",
        ["confirmation_id"],
    )
    op.create_unique_constraint(
        "uq_ai_prepared_action_execute_tool_call_id",
        "ai_prepared_action",
        ["execute_tool_call_id"],
    )
    op.create_unique_constraint(
        "uq_ai_op_log_tool_call_id",
        "ai_operation_log",
        ["tool_call_id"],
    )
    op.create_foreign_key(
        "ai_conversation_user_id_fkey",
        "ai_conversation",
        "sys_user",
        ["user_id"],
        ["user_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "ai_message_conversation_id_fkey",
        "ai_message",
        "ai_conversation",
        ["conversation_id"],
        ["conversation_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_ai_message_active_history",
        "ai_message",
        ["conversation_id", "create_time", "message_id"],
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
    )
    op.create_index(
        "ix_ai_prepared_action_conversation_status_expires",
        "ai_prepared_action",
        ["conversation_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_ai_prepared_action_source_status",
        "ai_prepared_action",
        ["source_user_message_id", "status"],
    )
    op.create_index(
        "ix_ai_operation_source_status",
        "ai_operation_log",
        ["source_user_message_id", "status"],
    )
    op.create_index("ix_ai_routing_log_create_time", "ai_routing_log", ["create_time"])
    op.create_index("ix_ai_routing_log_trace_id", "ai_routing_log", ["trace_id"])
    op.create_index("ix_ai_routing_log_user_id", "ai_routing_log", ["user_id"])
    op.create_index(
        "ix_ai_routing_feedback_create_time",
        "ai_routing_feedback",
        ["create_time"],
    )
    op.create_index(
        "ix_ai_routing_feedback_message_id",
        "ai_routing_feedback",
        ["message_id"],
    )
    op.create_index(
        "ix_ai_routing_feedback_trace_id",
        "ai_routing_feedback",
        ["trace_id"],
    )
    op.create_index(
        "ix_ai_routing_feedback_user_id",
        "ai_routing_feedback",
        ["user_id"],
    )

    op.alter_column(
        "ai_message",
        "tenant_id",
        existing_type=sa.BigInteger(),
        nullable=True,
        comment=None,
        existing_comment="租户ID；必须与所属会话一致",
    )
    op.alter_column(
        "ai_operation_log",
        "tenant_id",
        existing_type=sa.BigInteger(),
        nullable=False,
        comment=None,
        existing_comment="可信租户ID；历史单租户数据回填 0",
    )
    op.drop_column("ai_routing_feedback", "tenant_id")
    op.drop_column("ai_routing_log", "tenant_id")
    op.drop_column("ai_conversation", "tenant_id")
