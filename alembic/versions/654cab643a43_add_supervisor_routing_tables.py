"""add supervisor routing tables

新增 Supervisor 路由相关 schema：
1. ai_message 增加 agent_code / routing_feedback 列和 CHECK 约束
2. 新建 ai_routing_log 路由决策审计表
3. 新建 ai_routing_feedback 用户反馈历史表
4. 回填 ai_message.agent_code

对应 spec docs/superpowers/specs/2026-07-24-multi-agent-supervisor-routing-design.md。

Revision ID: 654cab643a43
Revises: 51e74fc18eb0
Create Date: 2026-07-25 13:00:40.383966

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "654cab643a43"
down_revision: str | Sequence[str] | None = "51e74fc18eb0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    1. ai_message 增加列和 CHECK 约束
    2. 新建 ai_routing_log
    3. 新建 ai_routing_feedback
    4. 回填 ai_message.agent_code
    """
    # ============ 1. ai_message 增加列和 CHECK ============
    op.add_column(
        "ai_message",
        sa.Column(
            "agent_code",
            sa.String(length=64),
            nullable=True,
            comment="本条消息实际处理的 Agent code，用于按消息粒度还原 Agent",
        ),
    )
    op.add_column(
        "ai_message",
        sa.Column(
            "routing_feedback",
            sa.String(length=16),
            nullable=True,
            comment="用户路由反馈：correct、wrong 或 null",
        ),
    )
    # autogenerate 漏检 __table_args__ 内的 CheckConstraint（已知问题），手工补
    op.create_check_constraint(
        "ck_ai_message_routing_feedback",
        "ai_message",
        "routing_feedback IS NULL OR routing_feedback IN ('correct', 'wrong')",
    )

    # ============ 2. 新建 ai_routing_log ============
    op.create_table(
        "ai_routing_log",
        sa.Column("log_id", sa.BigInteger(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "input_message_hash",
            sa.String(length=128),
            nullable=False,
            comment=(
                "HMAC-SHA256(server_secret + user_id + message)；运维调试用，非法证取证"
            ),
        ),
        sa.Column(
            "candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("llm_choice", sa.String(length=64), nullable=True),
        sa.Column("final_agent", sa.String(length=64), nullable=True),
        sa.Column(
            "reason",
            sa.String(length=64),
            nullable=False,
            comment=(
                "llm_resolved / clarification / session_sticky / manual_override / "
                "supervisor_disabled / safety_blocked / quota_exceeded / no_provider / "
                "no_candidates / legacy_null_mode"
            ),
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "parent_log_id",
            sa.BigInteger(),
            nullable=True,
            comment="为多 Agent 协作预留；当前始终为 NULL",
        ),
        sa.Column(
            "plan_step_index",
            sa.Integer(),
            nullable=True,
            comment="为多 Agent 协作预留；当前始终为 NULL",
        ),
        sa.Column(
            "create_time",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("log_id"),
        comment="Supervisor 路由决策审计日志",
    )
    op.create_index(
        op.f("ix_ai_routing_log_create_time"), "ai_routing_log", ["create_time"]
    )
    op.create_index(op.f("ix_ai_routing_log_trace_id"), "ai_routing_log", ["trace_id"])
    op.create_index(op.f("ix_ai_routing_log_user_id"), "ai_routing_log", ["user_id"])

    # ============ 3. 新建 ai_routing_feedback ============
    op.create_table(
        "ai_routing_feedback",
        sa.Column("feedback_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("original_agent", sa.String(length=64), nullable=False),
        sa.Column("feedback", sa.String(length=16), nullable=False),
        sa.Column("corrected_agent", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column(
            "create_time",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "feedback IN ('correct', 'wrong')",
            name="ck_ai_routing_feedback_type",
        ),
        sa.CheckConstraint(
            "(feedback = 'wrong' AND corrected_agent IS NOT NULL) "
            "OR (feedback = 'correct' AND corrected_agent IS NULL)",
            name="ck_ai_routing_feedback_correction_match",
        ),
        sa.PrimaryKeyConstraint("feedback_id"),
        comment="用户对路由决策的反馈历史轨迹（append-only）",
    )
    op.create_index(
        op.f("ix_ai_routing_feedback_create_time"),
        "ai_routing_feedback",
        ["create_time"],
    )
    op.create_index(
        op.f("ix_ai_routing_feedback_message_id"),
        "ai_routing_feedback",
        ["message_id"],
    )
    op.create_index(
        op.f("ix_ai_routing_feedback_trace_id"),
        "ai_routing_feedback",
        ["trace_id"],
    )
    op.create_index(
        op.f("ix_ai_routing_feedback_user_id"), "ai_routing_feedback", ["user_id"]
    )

    # ============ 4. 回填 ai_message.agent_code ============
    # 只回填 role='assistant' 的消息——user/tool 消息无 agent 归属语义；
    # 历史会话中途切 agent 时，user 消息不强行打标（避免近似错误扩大）.
    # 如果会话当前 agent_code 已变（比如切换过），所有历史 assistant 消息都打当前值，
    # 这是已知近似；新消息按消息粒度准确，历史数据允许不完全精确。
    op.execute(
        """
        UPDATE ai_message m
           SET agent_code = c.agent_code
          FROM ai_conversation c
         WHERE m.conversation_id = c.conversation_id
           AND c.agent_code IS NOT NULL
           AND m.role = 'assistant'
        """
    )


def downgrade() -> None:
    """Downgrade schema — 对称回滚，先 drop CHECK / 表，再 drop 列."""
    # ============ 3. drop ai_routing_feedback 表 + 索引 ============
    op.drop_index(
        op.f("ix_ai_routing_feedback_user_id"), table_name="ai_routing_feedback"
    )
    op.drop_index(
        op.f("ix_ai_routing_feedback_trace_id"), table_name="ai_routing_feedback"
    )
    op.drop_index(
        op.f("ix_ai_routing_feedback_message_id"), table_name="ai_routing_feedback"
    )
    op.drop_index(
        op.f("ix_ai_routing_feedback_create_time"), table_name="ai_routing_feedback"
    )
    op.drop_table("ai_routing_feedback")

    # ============ 2. drop ai_routing_log 表 + 索引 ============
    op.drop_index(op.f("ix_ai_routing_log_user_id"), table_name="ai_routing_log")
    op.drop_index(op.f("ix_ai_routing_log_trace_id"), table_name="ai_routing_log")
    op.drop_index(op.f("ix_ai_routing_log_create_time"), table_name="ai_routing_log")
    op.drop_table("ai_routing_log")

    # ============ 1. drop ai_message CHECK + 列 ============
    op.drop_constraint("ck_ai_message_routing_feedback", "ai_message", type_="check")
    op.drop_column("ai_message", "routing_feedback")
    op.drop_column("ai_message", "agent_code")
