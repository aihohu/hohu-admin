"""Add the governed AI management schema after the v0.1.4 boundary.

Revision ID: c7d8e9f0a1b2
Revises: 0b2165376771
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = "0b2165376771"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade the governed AI management schema."""
    _upgrade_tool_gateway()
    _upgrade_operation_timing()
    _upgrade_agent_quota()
    _upgrade_agent_risk()
    _upgrade_supervisor_routing()
    _upgrade_chat_causality()
    _upgrade_prepared_action()
    _upgrade_prepared_action_runtime()
    _upgrade_prepared_action_execution_lease()
    _upgrade_operation_tenant()
    _upgrade_authorization_lineage()
    _upgrade_message_projection_dependencies()
    _upgrade_prepared_projection_dependencies()
    _upgrade_phase4_trace()
    _upgrade_conversation_soft_delete()


def downgrade() -> None:
    """Downgrade the governed AI management schema."""
    _downgrade_conversation_soft_delete()
    _downgrade_phase4_trace()
    _downgrade_prepared_projection_dependencies()
    _downgrade_message_projection_dependencies()
    _downgrade_authorization_lineage()
    _downgrade_operation_tenant()
    _downgrade_prepared_action_execution_lease()
    _downgrade_prepared_action_runtime()
    _downgrade_prepared_action()
    _downgrade_chat_causality()
    _downgrade_supervisor_routing()
    _downgrade_agent_risk()
    _downgrade_agent_quota()
    _downgrade_operation_timing()
    _downgrade_tool_gateway()


def _upgrade_tool_gateway() -> None:
    """Upgrade schema."""
    # Create the AI agent catalog.
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

    # Create explicit role-to-agent assignments.
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

    # Create the AI operation audit log.
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

    # Add gateway anchors to existing conversation and message tables.
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

    # Add query and audit indexes.
    # Resolve message streams by conversation and trace.
    op.create_index(
        "idx_ai_message_conv_trace",
        "ai_message",
        ["conversation_id", "trace_id"],
    )

    # Aggregate operation alerts by user and time window.
    op.create_index(
        "idx_ai_op_log_user_started",
        "ai_operation_log",
        ["user_id", "started_at"],
    )

    # Sort AI Trace results by operation time.
    op.create_index(
        "idx_ai_op_log_trace",
        "ai_operation_log",
        ["trace_id", "started_at"],
    )

    # Resolve operations by conversation.
    op.create_index(
        "idx_ai_op_log_conversation",
        "ai_operation_log",
        ["conversation_id"],
    )

    # Limit the security event index to flagged rows.
    op.create_index(
        "idx_ai_op_log_security",
        "ai_operation_log",
        ["started_at"],
        postgresql_where=sa.text("is_security_event = true"),
    )


def _upgrade_operation_timing() -> None:
    """Upgrade schema."""
    # Add queued_at and use the previous started_at value for existing rows.
    op.add_column(
        "ai_operation_log",
        sa.Column(
            "queued_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
            comment="行级创建时间（pending_confirmation 入库时刻）",
        ),
    )
    # Autonomous runs keep the nullable HITL wait duration empty.
    op.add_column(
        "ai_operation_log",
        sa.Column(
            "hitl_wait_ms",
            sa.Integer(),
            nullable=True,
            comment="HITL 等待耗时（autonomous 流为 None）",
        ),
    )
    # The application writes the nullable business start time explicitly.
    op.alter_column(
        "ai_operation_log",
        "started_at",
        existing_type=sa.DateTime(),
        nullable=True,
        comment="业务执行起点（HITL approve 后 / autonomous 入库后）",
        existing_comment="单次 tool 调用开始时间",
        server_default=None,
    )
    # Clarify that execution duration excludes HITL waiting time.
    op.alter_column(
        "ai_operation_log",
        "duration_ms",
        existing_type=sa.Integer(),
        comment="业务执行耗时，不含 HITL 等待",
        existing_nullable=True,
    )

    # Replace started_at indexes with stable row-level queued_at indexes.
    op.drop_index("idx_ai_op_log_user_started", table_name="ai_operation_log")
    op.drop_index("idx_ai_op_log_trace", table_name="ai_operation_log")
    op.drop_index("idx_ai_op_log_security", table_name="ai_operation_log")
    op.create_index(
        "idx_ai_op_log_user_queued",
        "ai_operation_log",
        ["user_id", "queued_at"],
        unique=False,
    )
    op.create_index(
        "idx_ai_op_log_trace",
        "ai_operation_log",
        ["trace_id", "queued_at"],
        unique=False,
    )
    op.create_index(
        "idx_ai_op_log_security",
        "ai_operation_log",
        ["queued_at"],
        unique=False,
        postgresql_where=sa.text("is_security_event = true"),
    )


def _upgrade_agent_quota() -> None:
    op.add_column(
        "ai_agent",
        sa.Column(
            "daily_quota_per_user",
            sa.Integer(),
            nullable=True,
            comment="Agent 日配额上限，None 表示仅使用全局 L2 配额",
        ),
    )


def _upgrade_agent_risk() -> None:
    # Existing agents retain the equivalent balanced behavior.
    op.add_column(
        "ai_agent",
        sa.Column(
            "risk_appetite",
            sa.String(length=16),
            nullable=False,
            server_default="balanced",
            comment="风险偏好：conservative（high 永远 HITL）/ "
            "balanced（默认，high + dry_run_count≤1 autonomous）/ "
            "aggressive（high 永远 autonomous）。仅影响 high risk，"
            "destructive / hitl_always / injection_hit 不受影响",
        ),
    )
    # The database check complements the Python literal type.
    op.create_check_constraint(
        "ck_ai_agent_risk_appetite",
        "ai_agent",
        "risk_appetite IN ('conservative', 'balanced', 'aggressive')",
    )


def _upgrade_supervisor_routing() -> None:
    """Upgrade schema.

    1. ai_message 增加列和 CHECK 约束
    2. 新建 ai_routing_log
    3. 新建 ai_routing_feedback
    4. 回填 ai_message.agent_code
    """
    # Add routing fields and a consistency check to AI messages.
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
    # Alembic autogenerate misses this table_args check, so create it explicitly.
    op.create_check_constraint(
        "ck_ai_message_routing_feedback",
        "ai_message",
        "routing_feedback IS NULL OR routing_feedback IN ('correct', 'wrong')",
    )

    # Create immutable supervisor routing decisions.
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

    # Create append-only routing feedback.
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

    # Backfill only assistant messages because user and tool messages have no
    # agent ownership semantics. The current conversation agent is an accepted
    # approximation for historical assistant messages; new writes are exact.
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


def _upgrade_chat_causality() -> None:
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


def _upgrade_prepared_action() -> None:
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


def _upgrade_prepared_action_runtime() -> None:
    op.add_column(
        "ai_prepared_action",
        sa.Column("guard_owner_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ai_prepared_action",
        sa.Column(
            "command_action",
            sa.String(length=16),
            server_default=sa.text("'send'"),
            nullable=False,
        ),
    )
    op.add_column(
        "ai_prepared_action",
        sa.Column(
            "risk_level",
            sa.String(length=16),
            server_default=sa.text("'high'"),
            nullable=False,
        ),
    )
    op.add_column(
        "ai_prepared_action",
        sa.Column("chip_target", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "ai_prepared_action", sa.Column("result_data", sa.JSON(), nullable=True)
    )
    op.add_column(
        "ai_prepared_action", sa.Column("result_ui", sa.JSON(), nullable=True)
    )
    op.add_column(
        "ai_prepared_action", sa.Column("duration_ms", sa.Integer(), nullable=True)
    )


def _upgrade_prepared_action_execution_lease() -> None:
    op.add_column(
        "ai_prepared_action",
        sa.Column("execution_owner", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ai_prepared_action",
        sa.Column(
            "execution_lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def _upgrade_operation_tenant() -> None:
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
    # Future writes must provide tenant context after the legacy backfill.
    op.alter_column("ai_operation_log", "tenant_id", server_default=None)


def _upgrade_authorization_lineage() -> None:
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


def _upgrade_message_projection_dependencies() -> None:
    op.add_column(
        "ai_message",
        sa.Column(
            "projection_dependency_message_ids",
            sa.JSON(),
            nullable=True,
            comment="Immutable prior assistant message IDs used as model context",
        ),
    )


def _upgrade_prepared_projection_dependencies() -> None:
    op.add_column(
        "ai_prepared_action",
        sa.Column(
            "projection_dependency_message_ids",
            sa.JSON(),
            nullable=True,
            comment="Immutable prior assistant message IDs used as model context",
        ),
    )


def _upgrade_phase4_trace() -> None:
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


def _upgrade_conversation_soft_delete() -> None:
    op.add_column(
        "ai_conversation",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def _downgrade_conversation_soft_delete() -> None:
    op.drop_column("ai_conversation", "deleted_at")


def _downgrade_phase4_trace() -> None:
    op.drop_column("ai_operation_log", "target_summary")
    op.drop_column("ai_operation_log", "agent_code")


def _downgrade_prepared_projection_dependencies() -> None:
    op.drop_column("ai_prepared_action", "projection_dependency_message_ids")


def _downgrade_message_projection_dependencies() -> None:
    op.drop_column("ai_message", "projection_dependency_message_ids")


def _downgrade_authorization_lineage() -> None:
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


def _downgrade_operation_tenant() -> None:
    op.drop_index(
        "ix_ai_operation_tenant_queued_log",
        table_name="ai_operation_log",
    )
    op.drop_index("ix_ai_operation_tenant_trace", table_name="ai_operation_log")
    op.drop_column("ai_operation_log", "tenant_id")


def _downgrade_prepared_action_execution_lease() -> None:
    op.drop_column("ai_prepared_action", "execution_lease_expires_at")
    op.drop_column("ai_prepared_action", "execution_owner")


def _downgrade_prepared_action_runtime() -> None:
    op.drop_column("ai_prepared_action", "duration_ms")
    op.drop_column("ai_prepared_action", "result_ui")
    op.drop_column("ai_prepared_action", "result_data")
    op.drop_column("ai_prepared_action", "chip_target")
    op.drop_column("ai_prepared_action", "risk_level")
    op.drop_column("ai_prepared_action", "command_action")
    op.drop_column("ai_prepared_action", "guard_owner_token")


def _downgrade_prepared_action() -> None:
    op.drop_index(
        "ix_ai_prepared_action_source_status", table_name="ai_prepared_action"
    )
    op.drop_index(
        "ix_ai_prepared_action_conversation_status_expires",
        table_name="ai_prepared_action",
    )
    op.drop_table("ai_prepared_action")


def _downgrade_chat_causality() -> None:
    op.drop_index("ix_ai_operation_source_status", table_name="ai_operation_log")
    op.drop_index("ix_ai_message_supersedes_message_id", table_name="ai_message")
    op.drop_index("uq_ai_message_assistant_run", table_name="ai_message")
    op.drop_index("ix_ai_message_active_history", table_name="ai_message")
    op.drop_column("ai_operation_log", "readonly_snapshot")
    op.drop_column("ai_operation_log", "source_user_message_id")
    op.drop_column("ai_message", "supersedes_message_id")
    op.drop_column("ai_message", "is_active")


def _downgrade_supervisor_routing() -> None:
    """Drop routing checks, tables, and columns in dependency order."""
    # Drop routing feedback indexes and the table.
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

    # Drop routing log indexes and the table.
    op.drop_index(op.f("ix_ai_routing_log_user_id"), table_name="ai_routing_log")
    op.drop_index(op.f("ix_ai_routing_log_trace_id"), table_name="ai_routing_log")
    op.drop_index(op.f("ix_ai_routing_log_create_time"), table_name="ai_routing_log")
    op.drop_table("ai_routing_log")

    # Drop the message routing check and columns.
    op.drop_constraint("ck_ai_message_routing_feedback", "ai_message", type_="check")
    op.drop_column("ai_message", "routing_feedback")
    op.drop_column("ai_message", "agent_code")


def _downgrade_agent_risk() -> None:
    op.drop_constraint("ck_ai_agent_risk_appetite", "ai_agent", type_="check")
    op.drop_column("ai_agent", "risk_appetite")


def _downgrade_agent_quota() -> None:
    op.drop_column("ai_agent", "daily_quota_per_user")


def _downgrade_operation_timing() -> None:
    """Downgrade schema."""
    # Restore the original started_at indexes.
    op.drop_index("idx_ai_op_log_security", table_name="ai_operation_log")
    op.drop_index("idx_ai_op_log_trace", table_name="ai_operation_log")
    op.drop_index("idx_ai_op_log_user_queued", table_name="ai_operation_log")
    op.create_index(
        "idx_ai_op_log_security",
        "ai_operation_log",
        ["started_at"],
        unique=False,
        postgresql_where=sa.text("is_security_event = true"),
    )
    op.create_index(
        "idx_ai_op_log_trace",
        "ai_operation_log",
        ["trace_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "idx_ai_op_log_user_started",
        "ai_operation_log",
        ["user_id", "started_at"],
        unique=False,
    )

    # Restore the original duration comment.
    op.alter_column(
        "ai_operation_log",
        "duration_ms",
        existing_type=sa.Integer(),
        comment=None,
        existing_nullable=True,
    )
    # Restore the non-null started_at column and server default.
    op.alter_column(
        "ai_operation_log",
        "started_at",
        existing_type=sa.DateTime(),
        nullable=False,
        comment="单次 tool 调用开始时间",
        existing_comment="业务执行起点（HITL approve 后 / autonomous 入库后）",
        server_default=sa.text("now()"),
    )
    # Drop the timing columns introduced by this step.
    op.drop_column("ai_operation_log", "hitl_wait_ms")
    op.drop_column("ai_operation_log", "queued_at")


def _downgrade_tool_gateway() -> None:
    """Drop gateway indexes, columns, and tables in dependency order."""
    # Drop query and audit indexes.
    op.drop_index("idx_ai_op_log_security", table_name="ai_operation_log")
    op.drop_index("idx_ai_op_log_conversation", table_name="ai_operation_log")
    op.drop_index("idx_ai_op_log_trace", table_name="ai_operation_log")
    op.drop_index("idx_ai_op_log_user_started", table_name="ai_operation_log")
    op.drop_index("idx_ai_message_conv_trace", table_name="ai_message")

    # Drop gateway anchors from existing tables.
    op.drop_column("ai_message", "trace_id")
    op.drop_column("ai_conversation", "trace_id")
    op.drop_column("ai_conversation", "agent_code")

    # Drop gateway tables with dependents first.
    op.drop_table("ai_operation_log")
    op.drop_table("role_ai_agent")
    op.drop_table("ai_agent")
