"""add ai tool gateway tables

新增 AI Tool Gateway 三张表（ai_agent / role_ai_agent / ai_operation_log），
ALTER 现有 ai_conversation / ai_message 加 trace_id 字段。

创建 AI Tool Gateway 所需的 Agent、角色绑定、操作日志和会话字段。

Revision ID: c7d8e9f0a1b2
Revises: bf244f9a8b76
Create Date: 2026-07-03 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = "bf244f9a8b76"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ============ 1. 新建 ai_agent 表 ============
    op.create_table(
        "ai_agent",
        sa.Column("agent_id", sa.BigInteger(), nullable=False, comment="AgentID"),
        sa.Column(
            "code",
            sa.String(length=64),
            nullable=False,
            comment="Agent code，如 'user_mgmt' / 'shared'，与 @ai_tool(agent=...) 对应",
        ),
        sa.Column("name", sa.String(length=128), nullable=False, comment="显示名"),
        sa.Column("description", sa.Text(), nullable=False, comment="描述"),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="全局开关，默认禁用，部署方按需启用",
        ),
        sa.Column(
            "is_builtin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="是否内置 Agent（开源项目自带），UI 不允许删除",
        ),
        sa.Column(
            "display_order",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="排序",
        ),
        sa.Column(
            "system_prompt",
            sa.Text(),
            nullable=False,
            server_default="",
            comment="管理员 custom prompt，与固定 SAFETY_PREAMBLE 拼接，应用层限制 32KB",
        ),
        sa.Column(
            "model_preference",
            sa.String(length=128),
            nullable=True,
            comment="格式 'provider:model'，会话创建时作默认值，None=用全局默认",
        ),
        sa.Column(
            "create_time",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
            comment="创建时间",
        ),
        sa.Column(
            "update_time",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
            comment="更新时间",
        ),
        sa.PrimaryKeyConstraint("agent_id"),
        sa.UniqueConstraint("code", name="uq_ai_agent_code"),
        comment="AI Agent 注册中心",
    )

    # ============ 2. 新建 role_ai_agent 关联表 ============
    op.create_table(
        "role_ai_agent",
        sa.Column(
            "role_id",
            sa.BigInteger(),
            nullable=False,
            comment="角色ID",
        ),
        sa.Column(
            "agent_id",
            sa.BigInteger(),
            nullable=False,
            comment="AgentID",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
            comment="role 级软禁用，false=该角色用户看不到此 Agent",
        ),
        sa.Column(
            "create_time",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
            comment="创建时间",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["ai_agent.agent_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["role_id"], ["sys_role.role_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "agent_id"),
        comment="角色 ↔ Agent RBAC 关联表",
    )

    # ============ 3. 新建 ai_operation_log 表 ============
    op.create_table(
        "ai_operation_log",
        sa.Column("log_id", sa.BigInteger(), nullable=False, comment="日志ID"),
        sa.Column(
            "trace_id",
            sa.String(length=64),
            nullable=False,
            comment="追踪ID，串联同对话多 tool",
        ),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False, comment="会话ID"),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="调用用户ID"),
        sa.Column(
            "tool_name", sa.String(length=128), nullable=False, comment="tool 全限定名"
        ),
        sa.Column(
            "tool_call_id",
            sa.String(length=64),
            nullable=False,
            comment="单次工具调用 ID，供兜底轮询使用",
        ),
        sa.Column(
            "args_hash",
            sa.String(length=64),
            nullable=False,
            comment="SHA256 完整 64 字符，不截断",
        ),
        sa.Column(
            "args_summary",
            sa.Text(),
            nullable=False,
            comment="仅元信息（tool + risk + mode + dry_run_count），不含 args 原值",
        ),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column(
            "risk_level",
            sa.String(length=16),
            nullable=False,
            comment="low / high / destructive",
        ),
        sa.Column(
            "execution_mode",
            sa.String(length=32),
            nullable=False,
            comment="autonomous / hitl",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="running / pending_confirmation / success / failed / rejected / expired",
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("confirmation_id", sa.String(length=64), nullable=True),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
            comment="单次 tool 调用开始时间",
        ),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
        sa.Column(
            "is_security_event",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="是否安全事件（注入命中 / Guardrail 命中）",
        ),
        sa.Column(
            "event_type",
            sa.String(length=64),
            nullable=True,
            comment="injection_pattern_matched / guardrail_keyword",
        ),
        sa.Column(
            "severity",
            sa.String(length=16),
            nullable=True,
            comment="info / warning / critical",
        ),
        sa.PrimaryKeyConstraint("log_id"),
        sa.UniqueConstraint("tool_call_id", name="uq_ai_op_log_tool_call_id"),
        comment="AI 操作日志 + 安全事件（合并表）",
    )

    # ============ 4. 为现有表增加字段 ============
    op.add_column(
        "ai_conversation",
        sa.Column(
            "agent_code",
            sa.String(length=64),
            nullable=True,
            comment="绑定的 Agent code",
        ),
    )
    op.add_column(
        "ai_conversation",
        sa.Column(
            "trace_id",
            sa.String(length=64),
            nullable=True,
            comment="会话级追踪ID，串联 ai_operation_log",
        ),
    )
    op.add_column(
        "ai_message",
        sa.Column(
            "trace_id",
            sa.String(length=64),
            nullable=True,
            comment="追踪ID，与 ai_operation_log 关联",
        ),
    )

    # ============ 5. 查询与审计索引 ============
    # ai_message: 按 conversation + trace 反查消息流
    op.create_index(
        "idx_ai_message_conv_trace",
        "ai_message",
        ["conversation_id", "trace_id"],
    )

    # ai_operation_log：支持按用户和时间窗聚合告警。
    op.create_index(
        "idx_ai_op_log_user_started",
        "ai_operation_log",
        ["user_id", "started_at"],
    )

    # ai_operation_log：支持 AI Trace 视图按时间排序。
    op.create_index(
        "idx_ai_op_log_trace",
        "ai_operation_log",
        ["trace_id", "started_at"],
    )

    # ai_operation_log: conversation 维度反查
    op.create_index(
        "idx_ai_op_log_conversation",
        "ai_operation_log",
        ["conversation_id"],
    )

    # ai_operation_log：安全事件统计的部分索引，仅覆盖 is_security_event=true。
    op.create_index(
        "idx_ai_op_log_security",
        "ai_operation_log",
        ["started_at"],
        postgresql_where=sa.text("is_security_event = true"),
    )


def downgrade() -> None:
    """Downgrade schema — 对称回滚，先 drop 索引 / 列，再 drop 表。"""
    # ============ 5. drop 索引 ============
    op.drop_index("idx_ai_op_log_security", table_name="ai_operation_log")
    op.drop_index("idx_ai_op_log_conversation", table_name="ai_operation_log")
    op.drop_index("idx_ai_op_log_trace", table_name="ai_operation_log")
    op.drop_index("idx_ai_op_log_user_started", table_name="ai_operation_log")
    op.drop_index("idx_ai_message_conv_trace", table_name="ai_message")

    # ============ 4. drop ALTER 字段 ============
    op.drop_column("ai_message", "trace_id")
    op.drop_column("ai_conversation", "trace_id")
    op.drop_column("ai_conversation", "agent_code")

    # ============ 3. drop 三张表（顺序：依赖方先） ============
    op.drop_table("ai_operation_log")
    op.drop_table("role_ai_agent")
    op.drop_table("ai_agent")
