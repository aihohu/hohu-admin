"""All unreleased changes after the v0.1.4 baseline, squashed to head

Revision ID: e7cc9aa08769
Revises: bf244f9a8b76
Create Date: 2026-09-21 14:08:01.665270

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e7cc9aa08769"
down_revision: Union[str, Sequence[str], None] = "bf244f9a8b76"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CREATE_APPEND_ONLY_FUNCTION = sa.text(
    """
    CREATE FUNCTION reject_platform_audit_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'sys_platform_audit_log is append-only'
            USING ERRCODE = '55000';
    END;
    $$ LANGUAGE plpgsql
    """
)

_CREATE_APPEND_ONLY_TRIGGER = sa.text(
    """
    CREATE TRIGGER trg_platform_audit_append_only
    BEFORE UPDATE OR DELETE ON sys_platform_audit_log
    FOR EACH ROW EXECUTE FUNCTION reject_platform_audit_mutation()
    """
)

_CREATE_PRINCIPAL_VERSION_FUNCTION = sa.text(
    """
    CREATE FUNCTION bump_platform_principal_security_version()
    RETURNS trigger AS $$
    BEGIN
        IF NEW.hashed_password IS DISTINCT FROM OLD.hashed_password
           OR NEW.status IS DISTINCT FROM OLD.status
           OR NEW.permissions IS DISTINCT FROM OLD.permissions THEN
            NEW.row_version := OLD.row_version + 1;
        ELSIF NEW.row_version < OLD.row_version THEN
            RAISE EXCEPTION 'platform principal row_version cannot decrease'
                USING ERRCODE = '23514';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
)

_CREATE_PRINCIPAL_VERSION_TRIGGER = sa.text(
    """
    CREATE TRIGGER trg_platform_principal_security_version
    BEFORE UPDATE ON sys_platform_principal
    FOR EACH ROW EXECUTE FUNCTION bump_platform_principal_security_version()
    """
)

_CREATE_LINEAGE_FUNCTION = sa.text(
    """
    CREATE FUNCTION validate_platform_audit_lineage()
    RETURNS trigger AS $$
    BEGIN
        IF NEW.event_type = 'completed' AND NOT EXISTS (
            SELECT 1
            FROM sys_platform_audit_log authorized
            WHERE authorized.audit_id = NEW.authorization_audit_id
              AND authorized.event_type = 'authorized'
              AND authorized.actor_principal_id = NEW.actor_principal_id
              AND authorized.actor_name = NEW.actor_name
              AND authorized.permission = NEW.permission
              AND authorized.method = NEW.method
              AND authorized.path = NEW.path
              AND authorized.reason = NEW.reason
              AND authorized.ticket_id = NEW.ticket_id
              AND authorized.correlation_id = NEW.correlation_id
              AND authorized.target_tenant_id IS NOT DISTINCT FROM NEW.target_tenant_id
        ) THEN
            RAISE EXCEPTION 'platform completion audit lineage is invalid'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
)

_CREATE_LINEAGE_TRIGGER = sa.text(
    """
    CREATE TRIGGER trg_platform_audit_validate_lineage
    BEFORE INSERT ON sys_platform_audit_log
    FOR EACH ROW EXECUTE FUNCTION validate_platform_audit_lineage()
    """
)

_CREATE_TENANT_VERSION_FUNCTION = sa.text(
    """
    CREATE FUNCTION bump_sys_tenant_security_version()
    RETURNS trigger AS $$
    BEGIN
        IF NEW.tenant_code IS DISTINCT FROM OLD.tenant_code THEN
            RAISE EXCEPTION 'tenant_code is immutable'
                USING ERRCODE = '23514';
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status
           OR NEW.lifecycle_state IS DISTINCT FROM OLD.lifecycle_state
           OR NEW.bootstrap_version IS DISTINCT FROM OLD.bootstrap_version THEN
            NEW.row_version := OLD.row_version + 1;
        ELSIF NEW.row_version < OLD.row_version THEN
            RAISE EXCEPTION 'tenant row_version cannot decrease'
                USING ERRCODE = '23514';
        END IF;
        NEW.updated_at := now();
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
)

_CREATE_TENANT_VERSION_TRIGGER = sa.text(
    """
    CREATE TRIGGER trg_sys_tenant_security_version
    BEFORE UPDATE ON sys_tenant
    FOR EACH ROW EXECUTE FUNCTION bump_sys_tenant_security_version()
    """
)


def upgrade() -> None:

    op.add_column(
        "ai_conversation",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
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
        "ai_conversation",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Soft deletion timestamp; NULL means active",
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
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须与所属会话一致",
        ),
    )
    op.add_column("ai_message", sa.Column("tool_codes", sa.JSON(), nullable=True))
    op.add_column("ai_message", sa.Column("subject_refs", sa.JSON(), nullable=True))
    op.add_column(
        "ai_message",
        sa.Column("subject_refs_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "ai_message", sa.Column("data_scope_hash", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "ai_message", sa.Column("resolver_version", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "ai_message",
        sa.Column(
            "projection_dependency_message_ids",
            sa.JSON(),
            nullable=True,
            comment="Immutable prior assistant message IDs used as model context",
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
    op.add_column(
        "sys_config",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_data_scope_demo",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_dept",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_dict_data",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_dict_type",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_file",
        sa.Column(
            "owner_user_id",
            sa.BigInteger(),
            nullable=True,
            comment="文件所有者用户ID（NULL 仅兼容无法回填的历史记录）",
        ),
    )
    op.add_column(
        "sys_file",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_job",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_job_log",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_job_log",
        sa.Column(
            "runner_id",
            sa.String(length=64),
            nullable=True,
            comment="写入此日志的执行进程标识（uuid4，孤儿守护用）",
        ),
    )
    op.add_column(
        "sys_login_log",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=True,
            comment="已定位租户；unresolved 登录失败保持 NULL",
        ),
    )
    op.add_column(
        "sys_login_log",
        sa.Column(
            "audit_scope",
            sa.String(length=16),
            server_default="unresolved",
            nullable=False,
            comment="tenant/platform/unresolved",
        ),
    )
    op.add_column(
        "sys_menu",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_operation_log",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户审计归属",
        ),
    )
    op.add_column(
        "sys_operation_log",
        sa.Column(
            "audit_scope",
            sa.String(length=16),
            server_default="tenant",
            nullable=False,
            comment="审计作用域",
        ),
    )
    op.add_column(
        "sys_role",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_role_dept",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_role_menu",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_user",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_user",
        sa.Column(
            "employee_no",
            sa.String(length=64),
            nullable=True,
            comment="员工工号，用于企业同步、LDAP 或 ERP 对接；UNIQUE 但允许多个 NULL",
        ),
    )
    op.add_column(
        "sys_user",
        sa.Column(
            "auth_version",
            sa.Integer(),
            server_default="1",
            nullable=False,
            comment="认证版本；安全敏感变更后递增以撤销既有会话",
        ),
    )
    op.add_column(
        "sys_user_dept",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.add_column(
        "sys_user_role",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
    )
    op.create_unique_constraint(
        "uq_ai_conversation_tenant_conversation_id",
        "ai_conversation",
        ["tenant_id", "conversation_id"],
    )
    op.create_unique_constraint(
        "uq_ai_message_tenant_message_id", "ai_message", ["tenant_id", "message_id"]
    )
    op.create_unique_constraint(
        "uq_sys_config_tenant_config_id", "sys_config", ["tenant_id", "config_id"]
    )
    op.create_unique_constraint(
        "uq_sys_config_tenant_config_key", "sys_config", ["tenant_id", "config_key"]
    )
    op.create_unique_constraint(
        "uq_sys_data_scope_demo_tenant_demo_id",
        "sys_data_scope_demo",
        ["tenant_id", "demo_id"],
    )
    op.create_unique_constraint(
        "uq_sys_dept_tenant_dept_id", "sys_dept", ["tenant_id", "dept_id"]
    )
    op.create_unique_constraint(
        "uq_sys_dict_data_tenant_code", "sys_dict_data", ["tenant_id", "dict_code"]
    )
    op.create_unique_constraint(
        "uq_sys_dict_type_tenant_name", "sys_dict_type", ["tenant_id", "dict_name"]
    )
    op.create_unique_constraint(
        "uq_sys_dict_type_tenant_type", "sys_dict_type", ["tenant_id", "dict_type"]
    )
    op.create_unique_constraint(
        "uq_sys_dict_type_tenant_type_id",
        "sys_dict_type",
        ["tenant_id", "dict_type_id"],
    )
    op.create_unique_constraint(
        "uq_sys_file_tenant_file_id", "sys_file", ["tenant_id", "file_id"]
    )
    op.create_unique_constraint(
        "uq_sys_job_tenant_job_id", "sys_job", ["tenant_id", "job_id"]
    )
    op.create_unique_constraint(
        "uq_sys_job_tenant_job_key", "sys_job", ["tenant_id", "job_key"]
    )
    op.create_unique_constraint(
        "uq_sys_job_log_tenant_log_id", "sys_job_log", ["tenant_id", "job_log_id"]
    )
    op.create_unique_constraint(
        "uq_sys_login_log_tenant_log_id", "sys_login_log", ["tenant_id", "login_log_id"]
    )
    op.create_unique_constraint(
        "uq_sys_menu_tenant_menu_id", "sys_menu", ["tenant_id", "menu_id"]
    )
    op.create_unique_constraint(
        "uq_sys_operation_log_tenant_log_id",
        "sys_operation_log",
        ["tenant_id", "operation_log_id"],
    )
    op.create_unique_constraint(
        "uq_sys_role_tenant_role_code", "sys_role", ["tenant_id", "role_code"]
    )
    op.create_unique_constraint(
        "uq_sys_role_tenant_role_id", "sys_role", ["tenant_id", "role_id"]
    )
    op.create_unique_constraint(
        "uq_sys_role_tenant_role_name", "sys_role", ["tenant_id", "role_name"]
    )
    op.create_unique_constraint(
        "uq_sys_user_tenant_employee_no", "sys_user", ["tenant_id", "employee_no"]
    )
    op.create_unique_constraint(
        "uq_sys_user_tenant_user_id", "sys_user", ["tenant_id", "user_id"]
    )
    op.create_unique_constraint(
        "uq_sys_user_tenant_user_name", "sys_user", ["tenant_id", "user_name"]
    )
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
            comment="全局开关；ORM 默认禁用，fresh seed 按阶段发布集合决定",
        ),
        sa.Column(
            "is_builtin",
            sa.Boolean(),
            nullable=False,
            comment="是否内置 Agent（开源项目自带），UI 不允许删除",
        ),
        sa.Column("display_order", sa.Integer(), nullable=False, comment="排序"),
        sa.Column(
            "system_prompt",
            sa.Text(),
            nullable=False,
            comment="管理员 custom prompt，与固定 SAFETY_PREAMBLE 拼接，应用层限制 32KB",
        ),
        sa.Column(
            "model_preference",
            sa.String(length=128),
            nullable=True,
            comment="格式 'provider:model'，会话创建时作默认值，None=用全局默认",
        ),
        sa.Column(
            "daily_quota_per_user",
            sa.Integer(),
            nullable=True,
            comment="Agent 日配额上限，None 表示仅使用全局 L2 配额",
        ),
        sa.Column(
            "risk_appetite",
            sa.String(length=16),
            nullable=False,
            comment="风险偏好：conservative（high 永远 HITL）/ balanced（默认，high + dry_run_count≤1 autonomous）/ aggressive（high 永远 autonomous）。仅影响 high risk，destructive / hitl_always / injection_hit 不受影响",
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
        sa.UniqueConstraint("code"),
        comment="AI Agent 注册中心",
    )
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
        sa.Column(
            "source_user_message_id",
            sa.BigInteger(),
            nullable=True,
            comment="触发 operation 的 user message；NULL 仅兼容历史数据",
        ),
        sa.Column(
            "readonly_snapshot",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="执行时 AiToolMeta.readonly 快照；未知按 write 处理",
        ),
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=False,
            comment="可信租户ID；历史单租户数据回填 0",
        ),
        sa.Column(
            "agent_code",
            sa.String(length=64),
            nullable=True,
            comment="Immutable executing Agent code; NULL means unknown legacy data",
        ),
        sa.Column(
            "target_summary",
            sa.Text(),
            nullable=True,
            comment="Allowlisted immutable target references for audit display",
        ),
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
        sa.Column(
            "result_summary",
            sa.Text(),
            nullable=True,
            comment="status + affected_count + duration_ms + error_code",
        ),
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
            "queued_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
            comment="行级创建时间（pending_confirmation 入库时刻）",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(),
            nullable=True,
            comment="业务执行起点（HITL approve 后 / autonomous 入库后）",
        ),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            nullable=True,
            comment="业务执行耗时，不含 HITL 等待",
        ),
        sa.Column(
            "hitl_wait_ms",
            sa.Integer(),
            nullable=True,
            comment="HITL 等待耗时（autonomous 流为 None）",
        ),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
        sa.Column(
            "is_security_event",
            sa.Boolean(),
            nullable=False,
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
        sa.UniqueConstraint(
            "tenant_id", "tool_call_id", name="uq_ai_operation_log_tenant_tool_call_id"
        ),
        comment="AI 操作日志 + 安全事件（合并表）",
    )
    op.create_table(
        "ai_prepared_action",
        sa.Column(
            "action_id", sa.BigInteger(), nullable=False, comment="Snowflake action ID"
        ),
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
        sa.Column("tool_codes", sa.JSON(), nullable=True),
        sa.Column("subject_refs", sa.JSON(), nullable=True),
        sa.Column("subject_refs_hash", sa.String(length=64), nullable=True),
        sa.Column("data_scope_hash", sa.String(length=64), nullable=True),
        sa.Column("resolver_version", sa.String(length=32), nullable=True),
        sa.Column(
            "projection_dependency_message_ids",
            sa.JSON(),
            nullable=True,
            comment="Immutable prior assistant message IDs used as model context",
        ),
        sa.Column("presentation", sa.JSON(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_user_message_id", sa.BigInteger(), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("agent_code", sa.String(length=64), nullable=False),
        sa.Column("resolved_model_id", sa.BigInteger(), nullable=True),
        sa.Column("resolved_provider_id", sa.BigInteger(), nullable=True),
        sa.Column("guard_owner_token", sa.String(length=64), nullable=True),
        sa.Column(
            "command_action",
            sa.String(length=16),
            server_default=sa.text("'send'"),
            nullable=False,
        ),
        sa.Column(
            "risk_level",
            sa.String(length=16),
            server_default=sa.text("'high'"),
            nullable=False,
        ),
        sa.Column("chip_target", sa.String(length=255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("result_data", sa.JSON(), nullable=True),
        sa.Column("result_ui", sa.JSON(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("execution_owner", sa.String(length=64), nullable=True),
        sa.Column(
            "execution_lease_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('prepared', 'pending_confirmation', 'approved', 'running', 'succeeded', 'failed', 'rejected', 'expired')",
            name="ck_ai_prepared_action_status",
        ),
        sa.PrimaryKeyConstraint("action_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "confirmation_id",
            name="uq_ai_prepared_action_tenant_confirmation_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "execute_tool_call_id",
            name="uq_ai_prepared_action_tenant_execute_tool_call_id",
        ),
    )
    op.create_table(
        "ai_routing_feedback",
        sa.Column("feedback_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=False,
            comment="租户ID；必须与反馈消息一致",
        ),
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
            "(feedback = 'wrong' AND corrected_agent IS NOT NULL) OR (feedback = 'correct' AND corrected_agent IS NULL)",
            name="ck_ai_routing_feedback_correction_match",
        ),
        sa.CheckConstraint(
            "feedback IN ('correct', 'wrong')", name="ck_ai_routing_feedback_type"
        ),
        sa.PrimaryKeyConstraint("feedback_id"),
        comment="用户对路由决策的反馈历史轨迹（append-only）",
    )
    op.create_table(
        "ai_routing_log",
        sa.Column("log_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "input_message_hash",
            sa.String(length=128),
            nullable=False,
            comment="HMAC-SHA256(server_secret + user_id + message)；运维调试用，非法证取证",
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
            comment="llm_resolved / clarification / session_sticky / manual_override / supervisor_disabled / safety_blocked / quota_exceeded / no_provider / no_candidates / legacy_null_mode",
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
    op.create_table(
        "sys_platform_principal",
        sa.Column("principal_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("principal_name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=2), server_default="1", nullable=False),
        sa.Column(
            "permissions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(permissions) = 'array'",
            name="ck_platform_principal_permissions_array",
        ),
        sa.CheckConstraint(
            "principal_name ~ '^[a-z][a-z0-9_-]{2,63}$'",
            name="ck_platform_principal_name_format",
        ),
        sa.CheckConstraint("status IN ('1', '2')", name="ck_platform_principal_status"),
        sa.CheckConstraint(
            "principal_name = lower(btrim(principal_name))",
            name="ck_platform_principal_normalized_name",
        ),
        sa.CheckConstraint(
            "row_version >= 1", name="ck_platform_principal_row_version"
        ),
        sa.PrimaryKeyConstraint("principal_id"),
        sa.UniqueConstraint(
            "principal_name", name="uq_platform_principal_principal_name"
        ),
    )
    op.create_table(
        "sys_tenant",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column(
            "tenant_code",
            sa.String(length=32),
            nullable=False,
            comment="稳定的小写租户代码",
        ),
        sa.Column(
            "tenant_name", sa.String(length=100), nullable=False, comment="租户展示名称"
        ),
        sa.Column(
            "status",
            sa.String(length=2),
            server_default="1",
            nullable=False,
            comment="状态",
        ),
        sa.Column(
            "lifecycle_state",
            sa.String(length=16),
            nullable=False,
            comment="active/prepared/disabled",
        ),
        sa.Column(
            "provisioning_key_hash",
            sa.String(length=64),
            nullable=True,
            comment="tenant prepare 幂等键 SHA-256",
        ),
        sa.Column(
            "provisioning_fingerprint",
            sa.String(length=64),
            nullable=True,
            comment="tenant prepare 规范化请求 SHA-256",
        ),
        sa.Column(
            "bootstrap_version",
            sa.Integer(),
            server_default="0",
            nullable=False,
            comment="租户原子引导版本；0=未引导，1=Plan 5-B-B 完成",
        ),
        sa.Column(
            "bootstrap_key_hash",
            sa.String(length=64),
            nullable=True,
            comment="tenant bootstrap 幂等键 SHA-256",
        ),
        sa.Column(
            "bootstrap_fingerprint",
            sa.String(length=64),
            nullable=True,
            comment="tenant bootstrap 请求 keyed-HMAC fingerprint",
        ),
        sa.Column(
            "row_version",
            sa.Integer(),
            server_default="1",
            nullable=False,
            comment="授权与缓存漂移检测版本",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(lifecycle_state = 'active' AND status = '1') OR (lifecycle_state IN ('prepared', 'disabled') AND status = '2')",
            name="ck_sys_tenant_lifecycle_status",
        ),
        sa.CheckConstraint(
            "(provisioning_key_hash IS NULL AND provisioning_fingerprint IS NULL) OR (provisioning_key_hash IS NOT NULL AND provisioning_fingerprint IS NOT NULL AND provisioning_key_hash ~ '^[0-9a-f]{64}$' AND provisioning_fingerprint ~ '^[0-9a-f]{64}$')",
            name="ck_sys_tenant_provisioning_hashes",
        ),
        sa.CheckConstraint(
            "(tenant_id = 0 AND bootstrap_version = 1 AND bootstrap_key_hash IS NULL AND bootstrap_fingerprint IS NULL) OR (tenant_id <> 0 AND bootstrap_version = 0 AND bootstrap_key_hash IS NULL AND bootstrap_fingerprint IS NULL) OR (tenant_id <> 0 AND bootstrap_version = 1 AND bootstrap_key_hash IS NOT NULL AND bootstrap_fingerprint IS NOT NULL AND bootstrap_key_hash ~ '^[0-9a-f]{64}$' AND bootstrap_fingerprint ~ '^[0-9a-f]{64}$')",
            name="ck_sys_tenant_bootstrap_state",
        ),
        sa.CheckConstraint(
            "btrim(tenant_name) <> '' AND tenant_name !~ '[[:cntrl:]]'",
            name="ck_sys_tenant_name_format",
        ),
        sa.CheckConstraint(
            "lifecycle_state <> 'active' OR bootstrap_version >= 1",
            name="ck_sys_tenant_active_bootstrapped",
        ),
        sa.CheckConstraint("status IN ('1', '2')", name="ck_sys_tenant_status"),
        sa.CheckConstraint(
            "tenant_code ~ '^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$'",
            name="ck_sys_tenant_code_format",
        ),
        sa.CheckConstraint("row_version >= 1", name="ck_sys_tenant_row_version"),
        sa.CheckConstraint("tenant_id >= 0", name="ck_sys_tenant_nonnegative_id"),
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.UniqueConstraint(
            "bootstrap_key_hash", name="uq_sys_tenant_bootstrap_key_hash"
        ),
        sa.UniqueConstraint(
            "provisioning_key_hash", name="uq_sys_tenant_provisioning_key_hash"
        ),
        sa.UniqueConstraint("tenant_code", name="uq_sys_tenant_tenant_code"),
        comment="平台全局租户注册表",
    )
    op.create_table(
        "sys_platform_audit_log",
        sa.Column("audit_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("authorization_audit_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_principal_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_name", sa.String(length=64), nullable=False),
        sa.Column("permission", sa.String(length=96), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=256), nullable=False),
        sa.Column("target_tenant_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column("ticket_id", sa.String(length=128), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column(
            "request_summary",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "result_summary",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("ip", sa.String(length=50), nullable=True),
        sa.Column("denial_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(event_type = 'authorized' AND status_code IS NULL AND duration_ms IS NULL AND denial_code IS NULL) OR (event_type = 'completed' AND status_code BETWEEN 100 AND 599 AND duration_ms IS NOT NULL AND denial_code IS NULL) OR (event_type = 'denied' AND status_code BETWEEN 400 AND 599 AND duration_ms IS NULL AND denial_code IS NOT NULL)",
            name="ck_platform_audit_event_fields",
        ),
        sa.CheckConstraint(
            "(event_type = 'completed' AND authorization_audit_id IS NOT NULL) OR (event_type IN ('authorized', 'denied') AND authorization_audit_id IS NULL)",
            name="ck_platform_audit_authorization_lineage",
        ),
        sa.CheckConstraint(
            "(request_summary IS NULL OR jsonb_typeof(request_summary) = 'object') AND (result_summary IS NULL OR jsonb_typeof(result_summary) = 'object')",
            name="ck_platform_audit_summary_objects",
        ),
        sa.CheckConstraint(
            "event_type = 'denied' OR (reason IS NOT NULL AND btrim(reason) <> '' AND ticket_id IS NOT NULL AND btrim(ticket_id) <> '' AND correlation_id IS NOT NULL AND btrim(correlation_id) <> '')",
            name="ck_platform_audit_required_context",
        ),
        sa.CheckConstraint(
            "event_type IN ('authorized', 'completed', 'denied')",
            name="ck_platform_audit_event_type",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0", name="ck_platform_audit_duration"
        ),
        sa.CheckConstraint(
            "target_tenant_id IS NULL OR target_tenant_id >= 0",
            name="ck_platform_audit_target_tenant_id",
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["sys_platform_principal.principal_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["authorization_audit_id"],
            ["sys_platform_audit_log.audit_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_table(
        "role_ai_agent",
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=False,
            comment="租户ID；Agent 本身保持 platform-global",
        ),
        sa.Column("role_id", sa.BigInteger(), nullable=False, comment="角色ID"),
        sa.Column("agent_id", sa.BigInteger(), nullable=False, comment="AgentID"),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
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
        sa.ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["sys_role.tenant_id", "sys_role.role_id"],
            name="fk_role_ai_agent_tenant_role",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "role_id", "agent_id"),
        comment="角色 ↔ Agent RBAC 关联表",
    )
    op.create_table(
        "sys_user_export_task",
        sa.Column(
            "export_id", sa.String(length=64), nullable=False, comment="Snowflake ID"
        ),
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
        sa.Column("operator_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "filter_snapshot",
            sa.JSON(),
            nullable=False,
            comment="filter 快照（含 accessible_dept_ids 解析后的部门 ID 集合），防事后改 filter 反查时漂移",
        ),
        sa.Column(
            "reason",
            sa.String(length=256),
            nullable=False,
            comment="导出的业务理由，与导入批次理由语义一致",
        ),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column(
            "file_storage_key",
            sa.String(length=512),
            nullable=True,
            comment="导出文件 storage_key（FileStorage Protocol）",
        ),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "CREATED",
                "RUNNING",
                "SUCCESS",
                "FAILED",
                "EXPIRED",
                name="export_task_status",
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=1024), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operator_id"],
            ["sys_user.tenant_id", "sys_user.user_id"],
            name="fk_sys_user_export_tenant_operator",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["sys_tenant.tenant_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("export_id"),
        sa.UniqueConstraint(
            "tenant_id", "export_id", name="uq_sys_user_export_tenant_export"
        ),
    )
    op.create_table(
        "sys_user_import_batch",
        sa.Column("batch_id", sa.String(length=64), nullable=False, comment="UUID"),
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
        sa.Column("operator_id", sa.BigInteger(), nullable=False),
        sa.Column("filename", sa.String(length=256), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "records_hash",
            sa.String(length=64),
            nullable=False,
            comment="records 序列化后的 sha256，执行时比对以防预览后字段被修改",
        ),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column(
            "preview_token",
            sa.String(length=64),
            nullable=False,
            comment="执行时用于反查批次并保证幂等",
        ),
        sa.Column("summary_new", sa.Integer(), nullable=False),
        sa.Column("summary_exists", sa.Integer(), nullable=False),
        sa.Column("summary_conflict", sa.Integer(), nullable=False),
        sa.Column("summary_out_of_scope", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("overwritten_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column(
            "failed_rows_file",
            sa.String(length=512),
            nullable=True,
            comment="失败行 Excel storage_key",
        ),
        sa.Column(
            "file_storage_key",
            sa.String(length=512),
            nullable=True,
            comment="原始上传文件的 storage key",
        ),
        sa.Column(
            "on_conflict",
            sa.String(length=16),
            nullable=False,
            comment="skip / overwrite / fail_fast",
        ),
        sa.Column(
            "reason",
            sa.String(length=256),
            nullable=False,
            comment="批量操作的业务理由，进入审计链路",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "CREATED",
                "PREVIEW_DONE",
                "RUNNING",
                "SUCCESS",
                "PARTIAL_SUCCESS",
                "FAILED",
                "EXPIRED",
                "CANCELLED",
                name="import_batch_status",
                create_constraint=True,
            ),
            nullable=False,
            comment="导入批次状态",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operator_id"],
            ["sys_user.tenant_id", "sys_user.user_id"],
            name="fk_sys_user_import_batch_tenant_operator",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["sys_tenant.tenant_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("batch_id"),
        sa.UniqueConstraint(
            "tenant_id", "batch_id", name="uq_sys_user_import_batch_tenant_batch"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "preview_token",
            name="uq_sys_user_import_batch_tenant_preview_token",
        ),
    )
    op.create_table(
        "tenant_ai_model_policy",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="获授权租户ID"),
        sa.Column(
            "model_id", sa.BigInteger(), nullable=False, comment="平台全局模型ID"
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
            comment="租户是否可使用该模型",
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
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
    op.create_table(
        "sys_user_import_batch_log",
        sa.Column(
            "log_id", sa.String(length=64), nullable=False, comment="Snowflake ID"
        ),
        sa.Column(
            "tenant_id",
            sa.BigInteger(),
            nullable=False,
            comment="租户ID；必须由可信 TenantContext 显式写入",
        ),
        sa.Column("batch_id", sa.String(length=64), nullable=False),
        sa.Column("operator_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "event",
            sa.String(length=32),
            nullable=False,
            comment="事件：CREATED/PREVIEW_DONE/EXECUTE_START/CHUNK_PROGRESS/EXECUTE_FINISH/EXECUTE_FAILED/EXPIRED/CANCELLED",
        ),
        sa.Column(
            "from_status",
            sa.Enum(
                "CREATED",
                "PREVIEW_DONE",
                "RUNNING",
                "SUCCESS",
                "PARTIAL_SUCCESS",
                "FAILED",
                "EXPIRED",
                "CANCELLED",
                name="import_batch_status",
            ),
            nullable=True,
        ),
        sa.Column(
            "to_status",
            sa.Enum(
                "CREATED",
                "PREVIEW_DONE",
                "RUNNING",
                "SUCCESS",
                "PARTIAL_SUCCESS",
                "FAILED",
                "EXPIRED",
                "CANCELLED",
                name="import_batch_status",
            ),
            nullable=True,
        ),
        sa.Column(
            "detail",
            sa.JSON(),
            nullable=False,
            comment="事件详情：chunk_index / chunk_size / failed_in_chunk / error_message / reason 等",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            ["sys_user_import_batch.tenant_id", "sys_user_import_batch.batch_id"],
            name="fk_sys_user_import_log_tenant_batch",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operator_id"],
            ["sys_user.tenant_id", "sys_user.user_id"],
            name="fk_sys_user_import_log_tenant_operator",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["sys_tenant.tenant_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("log_id"),
        sa.UniqueConstraint(
            "tenant_id", "log_id", name="uq_sys_user_import_log_tenant_log"
        ),
    )
    # Seed the default tenant BEFORE tenant FKs are created: legacy tables
    # get tenant_id=0 backfilled via server_default, and those FKs reference
    # this row (mirrors the original M1 migration's bulk_insert).
    op.execute(
        "INSERT INTO sys_tenant (tenant_id, tenant_code, tenant_name, status, lifecycle_state, bootstrap_version, row_version) "
        "VALUES (0, 'default', 'Default Tenant', '1', 'active', 1, 1) ON CONFLICT (tenant_id) DO NOTHING"
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
    op.create_foreign_key(
        None,
        "sys_config",
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_sys_data_scope_demo_tenant_dept",
        "sys_data_scope_demo",
        "sys_dept",
        ["tenant_id", "dept_id"],
        ["tenant_id", "dept_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        None,
        "sys_data_scope_demo",
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_sys_data_scope_demo_tenant_creator",
        "sys_data_scope_demo",
        "sys_user",
        ["tenant_id", "create_by"],
        ["tenant_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_sys_dept_tenant_parent",
        "sys_dept",
        "sys_dept",
        ["tenant_id", "parent_id"],
        ["tenant_id", "dept_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        None,
        "sys_dept",
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        None,
        "sys_dict_data",
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_sys_dict_data_tenant_type",
        "sys_dict_data",
        "sys_dict_type",
        ["tenant_id", "dict_type"],
        ["tenant_id", "dict_type"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        None,
        "sys_dict_type",
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        None,
        "sys_file",
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_sys_file_tenant_owner",
        "sys_file",
        "sys_user",
        ["tenant_id", "owner_user_id"],
        ["tenant_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        None, "sys_job", "sys_tenant", ["tenant_id"], ["tenant_id"], ondelete="RESTRICT"
    )
    op.create_foreign_key(
        "fk_sys_job_log_tenant_job",
        "sys_job_log",
        "sys_job",
        ["tenant_id", "job_id"],
        ["tenant_id", "job_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        None,
        "sys_job_log",
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        None,
        "sys_login_log",
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        None,
        "sys_menu",
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    # Legacy root menus used 0 instead of NULL before the parent constraint.
    op.execute(sa.text("UPDATE sys_menu SET parent_id = NULL WHERE parent_id = 0"))
    op.create_foreign_key(
        "fk_sys_menu_tenant_parent",
        "sys_menu",
        "sys_menu",
        ["tenant_id", "parent_id"],
        ["tenant_id", "menu_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        None,
        "sys_operation_log",
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        None,
        "sys_role",
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        None,
        "sys_role_dept",
        "sys_dept",
        ["tenant_id", "dept_id"],
        ["tenant_id", "dept_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        None,
        "sys_role_dept",
        "sys_role",
        ["tenant_id", "role_id"],
        ["tenant_id", "role_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        None,
        "sys_role_menu",
        "sys_menu",
        ["tenant_id", "menu_id"],
        ["tenant_id", "menu_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        None,
        "sys_role_menu",
        "sys_role",
        ["tenant_id", "role_id"],
        ["tenant_id", "role_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        None,
        "sys_user",
        "sys_tenant",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        None,
        "sys_user_dept",
        "sys_dept",
        ["tenant_id", "dept_id"],
        ["tenant_id", "dept_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        None,
        "sys_user_dept",
        "sys_user",
        ["tenant_id", "user_id"],
        ["tenant_id", "user_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        None,
        "sys_user_role",
        "sys_role",
        ["tenant_id", "role_id"],
        ["tenant_id", "role_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        None,
        "sys_user_role",
        "sys_user",
        ["tenant_id", "user_id"],
        ["tenant_id", "user_id"],
        ondelete="CASCADE",
    )
    # ### end Alembic commands ###

    op.create_index(
        "idx_ai_op_log_conversation",
        "ai_operation_log",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "idx_ai_op_log_security",
        "ai_operation_log",
        ["queued_at"],
        unique=False,
        postgresql_where=sa.text("is_security_event = true"),
    )
    op.create_index(
        "idx_ai_op_log_trace",
        "ai_operation_log",
        ["trace_id", "queued_at"],
        unique=False,
    )
    op.create_index(
        "idx_ai_op_log_user_queued",
        "ai_operation_log",
        ["user_id", "queued_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_operation_tenant_queued_log",
        "ai_operation_log",
        ["tenant_id", "queued_at", "log_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_operation_tenant_source_status",
        "ai_operation_log",
        ["tenant_id", "conversation_id", "source_user_message_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_ai_operation_tenant_trace",
        "ai_operation_log",
        ["tenant_id", "trace_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_prepared_action_tenant_conversation_status_expires",
        "ai_prepared_action",
        ["tenant_id", "conversation_id", "status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_prepared_action_tenant_source_status",
        "ai_prepared_action",
        ["tenant_id", "source_user_message_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_ai_routing_feedback_tenant_message_created",
        "ai_routing_feedback",
        ["tenant_id", "message_id", "create_time"],
        unique=False,
    )
    op.create_index(
        "ix_ai_routing_feedback_tenant_trace",
        "ai_routing_feedback",
        ["tenant_id", "trace_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_routing_log_tenant_trace",
        "ai_routing_log",
        ["tenant_id", "trace_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_routing_log_tenant_user_created",
        "ai_routing_log",
        ["tenant_id", "user_id", "create_time"],
        unique=False,
    )
    op.create_index(
        "ix_platform_principal_status",
        "sys_platform_principal",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_platform_audit_actor_time",
        "sys_platform_audit_log",
        ["actor_principal_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_platform_audit_correlation",
        "sys_platform_audit_log",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(
        "ix_platform_audit_target_time",
        "sys_platform_audit_log",
        ["target_tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_platform_audit_one_completion",
        "sys_platform_audit_log",
        ["authorization_audit_id"],
        unique=True,
        postgresql_where=sa.text("authorization_audit_id IS NOT NULL"),
    )
    op.create_index(
        "ix_role_ai_agent_tenant_agent",
        "role_ai_agent",
        ["tenant_id", "agent_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sys_user_export_task_created_at"),
        "sys_user_export_task",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sys_user_export_task_operator_id"),
        "sys_user_export_task",
        ["operator_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sys_user_export_task_status"),
        "sys_user_export_task",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_sys_user_export_tenant_status",
        "sys_user_export_task",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sys_user_import_batch_created_at"),
        "sys_user_import_batch",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sys_user_import_batch_operator_id"),
        "sys_user_import_batch",
        ["operator_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sys_user_import_batch_status"),
        "sys_user_import_batch",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_sys_user_import_batch_tenant_status",
        "sys_user_import_batch",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_ai_model_policy_tenant_enabled_model",
        "tenant_ai_model_policy",
        ["tenant_id", "enabled", "model_id"],
        unique=False,
    )
    op.create_index(
        "uq_tenant_ai_model_policy_enabled_default",
        "tenant_ai_model_policy",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("enabled = true AND is_default = true"),
    )
    op.create_index(
        op.f("ix_sys_user_import_batch_log_batch_id"),
        "sys_user_import_batch_log",
        ["batch_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sys_user_import_batch_log_created_at"),
        "sys_user_import_batch_log",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sys_user_import_batch_log_operator_id"),
        "sys_user_import_batch_log",
        ["operator_id"],
        unique=False,
    )
    op.create_index(
        "ix_sys_user_import_log_tenant_batch",
        "sys_user_import_batch_log",
        ["tenant_id", "batch_id"],
        unique=False,
    )
    op.alter_column(
        "ai_conversation",
        "conversation_id",
        existing_type=sa.BIGINT(),
        comment="会话ID",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_conversation",
        "user_id",
        existing_type=sa.BIGINT(),
        comment="所属用户",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_conversation",
        "title",
        existing_type=sa.VARCHAR(length=200),
        comment="会话标题",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_conversation",
        "model_name",
        existing_type=sa.VARCHAR(length=100),
        comment="使用的模型标识",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_conversation",
        "system_prompt",
        existing_type=sa.TEXT(),
        comment="系统提示词（Agent instructions）",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_conversation",
        "status",
        existing_type=sa.SMALLINT(),
        comment="状态：0=活跃, 1=归档",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_conversation",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "ai_conversation",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.create_index(
        "ix_ai_conversation_tenant_user_updated",
        "ai_conversation",
        ["tenant_id", "user_id", "update_time", "conversation_id"],
        unique=False,
    )
    op.drop_constraint(
        op.f("ai_conversation_user_id_fkey"), "ai_conversation", type_="foreignkey"
    )
    op.alter_column(
        "ai_message",
        "message_id",
        existing_type=sa.BIGINT(),
        comment="消息ID",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_message",
        "conversation_id",
        existing_type=sa.BIGINT(),
        comment="所属会话",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_message",
        "parent_message_id",
        existing_type=sa.BIGINT(),
        comment="父消息（工具调用关联链）",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_message",
        "role",
        existing_type=sa.VARCHAR(length=20),
        comment="角色：user / assistant / system / tool",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_message",
        "message_type",
        existing_type=sa.VARCHAR(length=20),
        comment="类型：text / tool_call / tool_result",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_message",
        "content",
        existing_type=sa.TEXT(),
        comment="消息内容",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_message",
        "tokens_input",
        existing_type=sa.INTEGER(),
        comment="输入 token 数",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_message",
        "tokens_output",
        existing_type=sa.INTEGER(),
        comment="输出 token 数",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_message",
        "parts",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        comment="结构化消息内容（含图片、文件等）",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_message",
        "tool_calls",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        comment="工具调用记录列表（名称、参数、结果）",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_message",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.create_index(
        "ix_ai_message_active_history",
        "ai_message",
        ["tenant_id", "conversation_id", "create_time", "message_id"],
        unique=False,
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "ix_ai_message_tenant_supersedes",
        "ai_message",
        ["tenant_id", "supersedes_message_id"],
        unique=False,
    )
    op.create_index(
        "uq_ai_message_assistant_run",
        "ai_message",
        ["tenant_id", "conversation_id", "trace_id"],
        unique=True,
        postgresql_where=sa.text("role = 'assistant' AND trace_id IS NOT NULL"),
    )
    op.drop_constraint(
        op.f("ai_message_conversation_id_fkey"), "ai_message", type_="foreignkey"
    )
    op.alter_column(
        "ai_model",
        "model_id",
        existing_type=sa.BIGINT(),
        comment="模型ID",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_model",
        "provider_id",
        existing_type=sa.BIGINT(),
        comment="所属提供商ID",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_model",
        "name",
        existing_type=sa.VARCHAR(length=100),
        comment="模型名称",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_model",
        "capabilities",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment='能力标签，如 ["text","vision","image-gen"]',
        existing_nullable=False,
    )
    op.alter_column(
        "ai_model",
        "base_url",
        existing_type=sa.VARCHAR(length=500),
        comment="模型级 API 地址（覆盖提供商默认）",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_model",
        "is_enabled",
        existing_type=sa.BOOLEAN(),
        comment="是否启用",
        existing_nullable=False,
        existing_server_default=sa.text("true"),
    )
    op.alter_column(
        "ai_model",
        "sort_order",
        existing_type=sa.INTEGER(),
        comment="排序（越小越靠前）",
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "ai_model",
        "config",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment="扩展配置",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_model",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_model",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        nullable=False,
        comment="创建时间",
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "ai_model",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        nullable=False,
        comment="更新时间",
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "ai_provider",
        "provider_id",
        existing_type=sa.BIGINT(),
        comment="提供商ID",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_provider",
        "provider_code",
        existing_type=sa.VARCHAR(length=50),
        comment="提供商标识：openai / anthropic / deepseek",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_provider",
        "name",
        existing_type=sa.VARCHAR(length=100),
        comment="显示名称",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_provider",
        "api_key",
        existing_type=sa.VARCHAR(length=500),
        comment="API Key（加密存储）",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_provider",
        "base_url",
        existing_type=sa.VARCHAR(length=500),
        comment="默认 API 地址",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_provider",
        "is_enabled",
        existing_type=sa.BOOLEAN(),
        comment="是否启用",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_provider",
        "config",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        comment="扩展配置",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_provider",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_provider",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "ai_provider",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app",
        "id",
        existing_type=sa.BIGINT(),
        comment="应用ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app",
        "tenant_id",
        existing_type=sa.BIGINT(),
        comment="租户 ID；必须由可信 TenantContext 显式写入",
        existing_nullable=False,
        existing_server_default=sa.text("'0'::bigint"),
    )
    op.alter_column(
        "mk_app",
        "name",
        existing_type=sa.VARCHAR(length=100),
        comment="应用名称",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app",
        "slug",
        existing_type=sa.VARCHAR(length=150),
        comment="URL slug（唯一）",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app",
        "type",
        existing_type=sa.VARCHAR(length=20),
        comment="应用类型: lowcode|frontend|backend|fullstack|theme|bundle",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app",
        "category",
        existing_type=sa.VARCHAR(length=30),
        comment="应用分类",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app",
        "description",
        existing_type=sa.TEXT(),
        comment="应用描述",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "icon",
        existing_type=sa.VARCHAR(length=500),
        comment="图标 URL（对象存储，禁止外链）",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "author_id",
        existing_type=sa.BIGINT(),
        comment="作者用户ID",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "author_name",
        existing_type=sa.VARCHAR(length=100),
        comment="作者名（冗余展示字段）",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "status",
        existing_type=sa.VARCHAR(length=20),
        comment="状态: draft|published|...",
        existing_nullable=False,
        existing_server_default=sa.text("'draft'::character varying"),
    )
    op.alter_column(
        "mk_app",
        "current_version_id",
        existing_type=sa.BIGINT(),
        comment="当前发布版本ID",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "homepage",
        existing_type=sa.VARCHAR(length=500),
        comment="主页 URL",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "license",
        existing_type=sa.VARCHAR(length=50),
        comment="开源协议",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "download_count",
        existing_type=sa.INTEGER(),
        comment="累计下载次数",
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "mk_app",
        "avg_rating",
        existing_type=sa.NUMERIC(precision=3, scale=1),
        comment="平均评分（0-5）",
        existing_nullable=False,
        existing_server_default=sa.text("0.0"),
    )
    op.alter_column(
        "mk_app",
        "rating_count",
        existing_type=sa.INTEGER(),
        comment="评分人数",
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "mk_app",
        "tags_text",
        existing_type=sa.TEXT(),
        comment="冗余：mk_app_tag 名称以空格拼接，用于 search_vector",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "created_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app",
        "updated_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app_permission",
        "id",
        existing_type=sa.BIGINT(),
        comment="权限ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_permission",
        "app_id",
        existing_type=sa.BIGINT(),
        comment="所属应用ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_permission",
        "type",
        existing_type=sa.VARCHAR(length=30),
        comment="权限类型: api|external_api|menu|db_table|...",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_permission",
        "detail",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment="权限详情（原始结构）",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_permission",
        "detail_hash",
        existing_type=sa.VARCHAR(length=64),
        comment="detail canonical JSON 的 SHA-256",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_permission",
        "detail_canonical",
        existing_type=sa.TEXT(),
        comment="审计字段：canonical JSON 文本，便于 hash 算法迁移",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_permission",
        "created_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app_rating",
        "id",
        existing_type=sa.BIGINT(),
        comment="评分ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_rating",
        "app_id",
        existing_type=sa.BIGINT(),
        comment="被评分应用ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_rating",
        "user_id",
        existing_type=sa.BIGINT(),
        comment="评分用户ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_rating",
        "rating",
        existing_type=sa.SMALLINT(),
        comment="评分（1-5）",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_rating",
        "comment",
        existing_type=sa.TEXT(),
        comment="评分评论",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_rating",
        "created_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app_rating",
        "updated_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app_review",
        "id",
        existing_type=sa.BIGINT(),
        comment="审核记录ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_review",
        "app_id",
        existing_type=sa.BIGINT(),
        comment="所属应用ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_review",
        "version_id",
        existing_type=sa.BIGINT(),
        comment="被审核版本ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_review",
        "rule_check_result",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment="规则引擎检查结果",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "rule_check_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment="规则检查完成时间",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "ai_risk_level",
        existing_type=sa.VARCHAR(length=10),
        comment="AI 风险等级: low|medium|high",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "ai_report",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment="AI 审核报告",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "ai_review_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment="AI 审核完成时间",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "human_status",
        existing_type=sa.VARCHAR(length=20),
        comment="人工审核状态: pending|approved|rejected",
        existing_nullable=False,
        existing_server_default=sa.text("'pending'::character varying"),
    )
    op.alter_column(
        "mk_app_review",
        "human_reviewer_id",
        existing_type=sa.BIGINT(),
        comment="人工审核人ID",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "human_comment",
        existing_type=sa.TEXT(),
        comment="人工审核意见",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "human_reviewed_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment="人工审核完成时间",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "final_status",
        existing_type=sa.VARCHAR(length=20),
        comment="最终审核状态: pending|approved|rejected",
        existing_nullable=False,
        existing_server_default=sa.text("'pending'::character varying"),
    )
    op.alter_column(
        "mk_app_review",
        "created_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app_review",
        "updated_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app_version",
        "id",
        existing_type=sa.BIGINT(),
        comment="版本ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_version",
        "app_id",
        existing_type=sa.BIGINT(),
        comment="所属应用ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_version",
        "version",
        existing_type=sa.VARCHAR(length=20),
        comment="语义化版本号（semver）",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_version",
        "changelog",
        existing_type=sa.TEXT(),
        comment="变更说明",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_version",
        "manifest",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment="应用清单（JSON）",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_version",
        "file_url",
        existing_type=sa.VARCHAR(length=500),
        comment="制品包 URL",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_version",
        "file_hash",
        existing_type=sa.VARCHAR(length=64),
        comment="制品包 SHA-256",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_version",
        "file_size",
        existing_type=sa.BIGINT(),
        comment="制品包大小（字节）",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_version",
        "review_status",
        existing_type=sa.VARCHAR(length=20),
        comment="审核状态: pending|approved|rejected",
        existing_nullable=False,
        existing_server_default=sa.text("'pending'::character varying"),
    )
    op.alter_column(
        "mk_app_version",
        "created_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.create_table_comment(
        "mk_app_version", "应用版本（每次发布一行）", existing_comment=None, schema=None
    )
    op.alter_column(
        "mk_tenant_app",
        "id",
        existing_type=sa.BIGINT(),
        comment="安装记录ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_tenant_app",
        "tenant_id",
        existing_type=sa.BIGINT(),
        comment="租户 ID；必须由可信 TenantContext 显式写入",
        existing_nullable=False,
        existing_server_default=sa.text("'0'::bigint"),
    )
    op.alter_column(
        "mk_tenant_app",
        "app_id",
        existing_type=sa.BIGINT(),
        comment="已安装应用ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_tenant_app",
        "installed_version",
        existing_type=sa.VARCHAR(length=20),
        comment="已安装版本号（semver 字符串）",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_tenant_app",
        "status",
        existing_type=sa.VARCHAR(length=20),
        comment="安装状态: installed|disabled|uninstalled",
        existing_nullable=False,
        existing_server_default=sa.text("'installed'::character varying"),
    )
    op.alter_column(
        "mk_tenant_app",
        "config",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment="安装配置（用户填写的）",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_tenant_app",
        "approved_permissions",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment="用户已批准的权限清单",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_tenant_app",
        "retained_table_names",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment="卸载后保留的数据表名清单",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_tenant_app",
        "has_data",
        existing_type=sa.BOOLEAN(),
        comment="是否存在业务数据（决定是否可硬删除）",
        existing_nullable=False,
        existing_server_default=sa.text("false"),
    )
    op.alter_column(
        "mk_tenant_app",
        "installed_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment="首次安装时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_tenant_app",
        "updated_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_config",
        "config_id",
        existing_type=sa.BIGINT(),
        comment="配置ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_config",
        "config_name",
        existing_type=sa.VARCHAR(length=100),
        comment="配置名称",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_config",
        "config_key",
        existing_type=sa.VARCHAR(length=100),
        comment="配置键",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_config",
        "config_value",
        existing_type=sa.TEXT(),
        comment="配置值",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_config",
        "config_type",
        existing_type=sa.VARCHAR(length=20),
        comment="配置类型：text/richtext/file",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_config",
        "config_group",
        existing_type=sa.VARCHAR(length=50),
        comment="配置分组",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_config",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_config",
        "is_public",
        existing_type=sa.BOOLEAN(),
        comment="是否公开访问",
        existing_nullable=False,
        existing_server_default=sa.text("false"),
    )
    op.alter_column(
        "sys_config",
        "remark",
        existing_type=sa.VARCHAR(length=500),
        comment="备注",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_config",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_config",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_config",
        "update_by",
        existing_type=sa.VARCHAR(length=64),
        comment="更新者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_config",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.drop_constraint(op.f("sys_config_config_key_key"), "sys_config", type_="unique")
    op.create_index(
        "ix_sys_config_tenant_group",
        "sys_config",
        ["tenant_id", "config_group"],
        unique=False,
    )
    op.alter_column(
        "sys_data_scope_demo",
        "demo_id",
        existing_type=sa.BIGINT(),
        comment="演示数据ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_data_scope_demo",
        "title",
        existing_type=sa.VARCHAR(length=100),
        comment="标题",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_data_scope_demo",
        "content",
        existing_type=sa.TEXT(),
        comment="内容",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_data_scope_demo",
        "dept_id",
        existing_type=sa.BIGINT(),
        comment="所属部门ID（数据权限锚点）",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_data_scope_demo",
        "create_by",
        existing_type=sa.BIGINT(),
        comment="创建人 user_id（SELF scope 锚点，存 ID 而非 user_name）",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_data_scope_demo",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_data_scope_demo",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_data_scope_demo",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.create_index(
        "ix_sys_data_scope_demo_tenant_creator",
        "sys_data_scope_demo",
        ["tenant_id", "create_by"],
        unique=False,
    )
    op.create_index(
        "ix_sys_data_scope_demo_tenant_dept",
        "sys_data_scope_demo",
        ["tenant_id", "dept_id"],
        unique=False,
    )
    op.alter_column(
        "sys_dept",
        "dept_id",
        existing_type=sa.BIGINT(),
        comment="部门ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dept",
        "parent_id",
        existing_type=sa.BIGINT(),
        comment="父部门ID",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "ancestors",
        existing_type=sa.VARCHAR(length=500),
        comment="祖先路径",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "dept_name",
        existing_type=sa.VARCHAR(length=100),
        comment="部门名称",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dept",
        "order_num",
        existing_type=sa.INTEGER(),
        comment="显示顺序",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dept",
        "leader",
        existing_type=sa.VARCHAR(length=50),
        comment="负责人",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "phone",
        existing_type=sa.VARCHAR(length=20),
        comment="联系电话",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "email",
        existing_type=sa.VARCHAR(length=100),
        comment="邮箱",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dept",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_dept",
        "update_by",
        existing_type=sa.VARCHAR(length=64),
        comment="更新者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.create_index(
        "ix_sys_dept_tenant_parent",
        "sys_dept",
        ["tenant_id", "parent_id"],
        unique=False,
    )
    op.create_index(
        "ix_sys_dept_tenant_status", "sys_dept", ["tenant_id", "status"], unique=False
    )
    op.alter_column(
        "sys_dict_data",
        "dict_code",
        existing_type=sa.BIGINT(),
        comment="字典编码",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_data",
        "dict_sort",
        existing_type=sa.INTEGER(),
        comment="字典排序",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_data",
        "dict_label",
        existing_type=sa.VARCHAR(length=100),
        comment="字典标签",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_data",
        "dict_value",
        existing_type=sa.VARCHAR(length=100),
        comment="字典键值",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_data",
        "dict_type",
        existing_type=sa.VARCHAR(length=100),
        comment="字典类型",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_data",
        "css_class",
        existing_type=sa.VARCHAR(length=100),
        comment="样式属性",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_data",
        "list_class",
        existing_type=sa.VARCHAR(length=100),
        comment="表格回显样式",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_data",
        "is_default",
        existing_type=sa.VARCHAR(length=2),
        comment="是否默认：Y-是，N-否",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_data",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_data",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_data",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_dict_data",
        "update_by",
        existing_type=sa.VARCHAR(length=64),
        comment="更新者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_data",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.create_index(
        "ix_sys_dict_data_tenant_type",
        "sys_dict_data",
        ["tenant_id", "dict_type"],
        unique=False,
    )
    op.alter_column(
        "sys_dict_type",
        "dict_type_id",
        existing_type=sa.BIGINT(),
        comment="字典类型ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_type",
        "dict_name",
        existing_type=sa.VARCHAR(length=100),
        comment="字典名称",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_type",
        "dict_type",
        existing_type=sa.VARCHAR(length=100),
        comment="字典类型",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_type",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_type",
        "remark",
        existing_type=sa.VARCHAR(length=500),
        comment="备注",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_type",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_type",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_dict_type",
        "update_by",
        existing_type=sa.VARCHAR(length=64),
        comment="更新者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_type",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.drop_constraint(
        op.f("sys_dict_type_dict_name_key"), "sys_dict_type", type_="unique"
    )
    op.drop_constraint(
        op.f("sys_dict_type_dict_type_key"), "sys_dict_type", type_="unique"
    )
    op.create_index(
        "ix_sys_dict_type_tenant_status",
        "sys_dict_type",
        ["tenant_id", "status"],
        unique=False,
    )
    op.alter_column(
        "sys_file",
        "file_id",
        existing_type=sa.BIGINT(),
        comment="文件ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "original_name",
        existing_type=sa.VARCHAR(length=255),
        comment="原始文件名",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "file_name",
        existing_type=sa.VARCHAR(length=255),
        comment="存储文件名(Snowflake ID)",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "file_path",
        existing_type=sa.VARCHAR(length=500),
        comment="相对路径",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "file_url",
        existing_type=sa.VARCHAR(length=500),
        comment="文件访问URL",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "file_size",
        existing_type=sa.BIGINT(),
        comment="文件大小(字节)",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "file_ext",
        existing_type=sa.VARCHAR(length=20),
        comment="文件扩展名",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "mime_type",
        existing_type=sa.VARCHAR(length=100),
        comment="MIME类型",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_file",
        "business_type",
        existing_type=sa.VARCHAR(length=50),
        comment="业务类型(如product、avatar)",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_file",
        "business_id",
        existing_type=sa.BIGINT(),
        comment="业务记录ID",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_file",
        "del_flag",
        existing_type=sa.VARCHAR(length=1),
        comment="删除标记: 0-正常, 1-已删除",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment="上传者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_file",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="上传时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.create_index(
        "ix_sys_file_tenant_deleted",
        "sys_file",
        ["tenant_id", "del_flag"],
        unique=False,
    )
    op.create_index(
        "ix_sys_file_tenant_owner",
        "sys_file",
        ["tenant_id", "owner_user_id"],
        unique=False,
    )
    op.alter_column(
        "sys_job",
        "job_id",
        existing_type=sa.BIGINT(),
        comment="任务ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job",
        "job_name",
        existing_type=sa.VARCHAR(length=64),
        comment="任务名称",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job",
        "job_key",
        existing_type=sa.VARCHAR(length=64),
        comment="任务标识",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job",
        "cron_expression",
        existing_type=sa.VARCHAR(length=64),
        comment="cron表达式",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "trigger_type",
        existing_type=sa.VARCHAR(length=10),
        comment="调度类型：cron-表达式，interval-间隔",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job",
        "interval_value",
        existing_type=sa.INTEGER(),
        comment="间隔值",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "interval_unit",
        existing_type=sa.VARCHAR(length=10),
        comment="间隔单位：seconds/minutes/hours/days",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "job_args",
        existing_type=sa.TEXT(),
        comment="任务参数JSON",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment="状态：1-启用，2-停用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job",
        "concurrent",
        existing_type=sa.VARCHAR(length=2),
        comment="并发策略：1-允许，2-不允许",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job",
        "timeout_seconds",
        existing_type=sa.INTEGER(),
        comment="单次执行超时秒数（空表示不限）",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "max_retries",
        existing_type=sa.INTEGER(),
        comment="失败重试次数（0 表示不重试）",
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "sys_job",
        "run_on_enable",
        existing_type=sa.BOOLEAN(),
        comment="启用时是否立即执行一次",
        existing_nullable=False,
        existing_server_default=sa.text("false"),
    )
    op.alter_column(
        "sys_job",
        "remark",
        existing_type=sa.VARCHAR(length=256),
        comment="备注",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_job",
        "update_by",
        existing_type=sa.VARCHAR(length=64),
        comment="更新者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.drop_constraint(op.f("sys_job_job_key_key"), "sys_job", type_="unique")
    op.create_index(
        "ix_sys_job_tenant_status", "sys_job", ["tenant_id", "status"], unique=False
    )
    op.alter_column(
        "sys_job_log",
        "job_log_id",
        existing_type=sa.BIGINT(),
        comment="日志ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job_log",
        "job_id",
        existing_type=sa.BIGINT(),
        comment="任务ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job_log",
        "job_name",
        existing_type=sa.VARCHAR(length=64),
        comment="任务名称",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job_log",
        "job_key",
        existing_type=sa.VARCHAR(length=64),
        comment="任务标识",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job_log",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment="状态：1-成功，2-失败，3-执行中",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job_log",
        "error_msg",
        existing_type=sa.TEXT(),
        comment="异常信息",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job_log",
        "start_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="开始时间",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job_log",
        "end_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="结束时间",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job_log",
        "duration",
        existing_type=sa.INTEGER(),
        comment="耗时（毫秒）",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job_log",
        "attempt_count",
        existing_type=sa.INTEGER(),
        comment="本次触发实际执行次数（含重试）",
        existing_nullable=False,
        existing_server_default=sa.text("1"),
    )
    op.create_index(
        "ix_sys_job_log_tenant_status_start",
        "sys_job_log",
        ["tenant_id", "status", "start_time"],
        unique=False,
    )
    op.alter_column(
        "sys_login_log",
        "login_log_id",
        existing_type=sa.BIGINT(),
        comment="日志ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_login_log",
        "user_id",
        existing_type=sa.BIGINT(),
        comment="登录用户ID",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_login_log",
        "username",
        existing_type=sa.VARCHAR(length=50),
        comment="登录用户名",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_login_log",
        "ip",
        existing_type=sa.VARCHAR(length=50),
        comment="登录IP",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_login_log",
        "user_agent",
        existing_type=sa.VARCHAR(length=500),
        comment="浏览器信息",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_login_log",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment="状态：1-成功，2-失败，3-锁定",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_login_log",
        "message",
        existing_type=sa.VARCHAR(length=200),
        comment="结果描述",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_login_log",
        "login_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="登录时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.drop_index(op.f("ix_login_log_login_time"), table_name="sys_login_log")
    op.create_index(
        "ix_login_log_scope_time",
        "sys_login_log",
        ["audit_scope", "login_time"],
        unique=False,
    )
    op.create_index(
        "ix_login_log_tenant_time",
        "sys_login_log",
        ["tenant_id", "login_time"],
        unique=False,
    )
    op.alter_column(
        "sys_menu",
        "menu_id",
        existing_type=sa.BIGINT(),
        comment="菜单ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_menu",
        "parent_id",
        existing_type=sa.BIGINT(),
        comment="父菜单ID",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "menu_name",
        existing_type=sa.VARCHAR(length=50),
        comment="菜单标题",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_menu",
        "menu_type",
        existing_type=sa.VARCHAR(length=1),
        comment="类型: M目录, C菜单, F按钮",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_menu",
        "icon",
        existing_type=sa.VARCHAR(length=50),
        comment="菜单图标",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "icon_type",
        existing_type=sa.VARCHAR(length=1),
        comment="菜单图标类型",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "path",
        existing_type=sa.VARCHAR(length=255),
        comment="路由路径",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "component",
        existing_type=sa.VARCHAR(length=255),
        comment="组件",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "route_name",
        existing_type=sa.VARCHAR(length=50),
        comment="前端路由名称（name）",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "route_path",
        existing_type=sa.VARCHAR(length=255),
        comment="前端路由路径",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "order",
        existing_type=sa.INTEGER(),
        comment="排序（越小越靠前）",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_menu",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_menu",
        "create_by",
        existing_type=sa.VARCHAR(length=32),
        comment="创建人",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "update_by",
        existing_type=sa.VARCHAR(length=32),
        comment="更新人",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_menu",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_menu",
        "path_param",
        existing_type=sa.VARCHAR(length=255),
        comment="路径参数",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "page",
        existing_type=sa.VARCHAR(length=255),
        comment="页面组件",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "layout",
        existing_type=sa.VARCHAR(length=255),
        comment="布局组件",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "i18n_key",
        existing_type=sa.VARCHAR(length=100),
        comment="国际化key",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "keep_alive",
        existing_type=sa.BOOLEAN(),
        comment="缓存路由",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "constant",
        existing_type=sa.BOOLEAN(),
        comment="常量路由",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "href",
        existing_type=sa.VARCHAR(length=255),
        comment="外链",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "hide_in_menu",
        existing_type=sa.BOOLEAN(),
        comment="隐藏菜单",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "active_menu",
        existing_type=sa.VARCHAR(length=50),
        comment="激活菜单的路由名称",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "multi_tab",
        existing_type=sa.BOOLEAN(),
        comment="是否支持多页签",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "fixed_index_in_tab",
        existing_type=sa.INTEGER(),
        comment="页签固定索引",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "permission",
        existing_type=sa.VARCHAR(length=50),
        comment="按钮/功能权限",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "query",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        comment="路由参数",
        existing_nullable=True,
    )
    op.create_index(
        "ix_sys_menu_tenant_parent",
        "sys_menu",
        ["tenant_id", "parent_id"],
        unique=False,
    )
    op.create_index(
        "ix_sys_menu_tenant_status", "sys_menu", ["tenant_id", "status"], unique=False
    )
    op.alter_column(
        "sys_operation_log",
        "operation_log_id",
        existing_type=sa.BIGINT(),
        comment="日志ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_operation_log",
        "user_id",
        existing_type=sa.BIGINT(),
        comment="操作人ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_operation_log",
        "username",
        existing_type=sa.VARCHAR(length=50),
        comment="操作人用户名",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_operation_log",
        "module",
        existing_type=sa.VARCHAR(length=50),
        comment="业务模块",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_operation_log",
        "action",
        existing_type=sa.VARCHAR(length=20),
        comment="操作类型",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_operation_log",
        "method",
        existing_type=sa.VARCHAR(length=10),
        comment="HTTP方法",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_operation_log",
        "path",
        existing_type=sa.VARCHAR(length=200),
        comment="请求路径",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_operation_log",
        "request_params",
        existing_type=sa.TEXT(),
        comment="请求参数摘要",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_operation_log",
        "status_code",
        existing_type=sa.INTEGER(),
        comment="响应状态码",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_operation_log",
        "ip",
        existing_type=sa.VARCHAR(length=50),
        comment="操作者IP",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_operation_log",
        "duration",
        existing_type=sa.INTEGER(),
        comment="耗时（毫秒）",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_operation_log",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="操作时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.drop_index(op.f("ix_operation_log_create_time"), table_name="sys_operation_log")
    op.drop_index(op.f("ix_operation_log_user_id"), table_name="sys_operation_log")
    op.create_index(
        "ix_operation_log_tenant_time",
        "sys_operation_log",
        ["tenant_id", "create_time"],
        unique=False,
    )
    op.create_index(
        "ix_operation_log_tenant_user",
        "sys_operation_log",
        ["tenant_id", "user_id"],
        unique=False,
    )
    op.alter_column(
        "sys_role",
        "role_id",
        existing_type=sa.BIGINT(),
        comment="角色ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_role",
        "role_name",
        existing_type=sa.VARCHAR(length=50),
        comment="角色名称",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_role",
        "role_code",
        existing_type=sa.VARCHAR(length=50),
        comment="角色编码",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_role",
        "role_desc",
        existing_type=sa.VARCHAR(length=255),
        comment="角色描述",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_role",
        "data_scope",
        existing_type=sa.VARCHAR(length=2),
        comment="数据权限范围：1-全部，2-自定义，3-本部门，4-本部门及以下，5-仅本人",
        existing_nullable=False,
        existing_server_default=sa.text("'1'::character varying"),
    )
    op.alter_column(
        "sys_role",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_role",
        "create_by",
        existing_type=sa.VARCHAR(length=32),
        comment="创建人",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_role",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_role",
        "update_by",
        existing_type=sa.VARCHAR(length=64),
        comment="更新人",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_role",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.drop_constraint(op.f("sys_role_role_code_key"), "sys_role", type_="unique")
    op.drop_constraint(op.f("sys_role_role_name_key"), "sys_role", type_="unique")
    op.create_index(
        "ix_sys_role_tenant_status", "sys_role", ["tenant_id", "status"], unique=False
    )
    op.create_index(
        "ix_sys_role_dept_tenant_dept",
        "sys_role_dept",
        ["tenant_id", "dept_id"],
        unique=False,
    )
    op.drop_constraint(
        op.f("sys_role_dept_role_id_fkey"), "sys_role_dept", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("sys_role_dept_dept_id_fkey"), "sys_role_dept", type_="foreignkey"
    )
    op.create_index(
        "ix_sys_role_menu_tenant_menu",
        "sys_role_menu",
        ["tenant_id", "menu_id"],
        unique=False,
    )
    op.drop_constraint(
        op.f("sys_role_menu_menu_id_fkey"), "sys_role_menu", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("sys_role_menu_role_id_fkey"), "sys_role_menu", type_="foreignkey"
    )
    op.alter_column(
        "sys_user",
        "user_id",
        existing_type=sa.BIGINT(),
        comment="用户ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_user",
        "user_name",
        existing_type=sa.VARCHAR(length=50),
        comment="账号",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_user",
        "nickname",
        existing_type=sa.VARCHAR(length=50),
        comment="昵称",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_user",
        "hashed_password",
        existing_type=sa.VARCHAR(length=255),
        comment="加密密码",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_user",
        "status",
        existing_type=sa.VARCHAR(length=10),
        comment="状态",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_user",
        "user_avatar",
        existing_type=sa.VARCHAR(length=255),
        comment="头像地址",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_user",
        "user_email",
        existing_type=sa.VARCHAR(length=100),
        comment="邮箱",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_user",
        "user_phone",
        existing_type=sa.VARCHAR(length=20),
        comment="手机号",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_user",
        "user_gender",
        existing_type=sa.VARCHAR(length=1),
        comment="用户性别: 0:未知,1:男,2:女",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_user",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_user",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.drop_index(op.f("ix_sys_user_user_name"), table_name="sys_user")
    op.create_index(
        op.f("ix_sys_user_tenant_id"), "sys_user", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_sys_user_tenant_status", "sys_user", ["tenant_id", "status"], unique=False
    )
    op.create_index(
        "ix_sys_user_dept_tenant_dept",
        "sys_user_dept",
        ["tenant_id", "dept_id"],
        unique=False,
    )
    op.drop_constraint(
        op.f("sys_user_dept_dept_id_fkey"), "sys_user_dept", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("sys_user_dept_user_id_fkey"), "sys_user_dept", type_="foreignkey"
    )
    op.create_index(
        "ix_sys_user_role_tenant_role",
        "sys_user_role",
        ["tenant_id", "role_id"],
        unique=False,
    )
    op.drop_constraint(
        op.f("sys_user_role_role_id_fkey"), "sys_user_role", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("sys_user_role_user_id_fkey"), "sys_user_role", type_="foreignkey"
    )

    # --- Align with the pre-squash chain head: server defaults, tenant-scoped PKs, check constraints. ---
    op.execute("ALTER TABLE sys_role_dept DROP CONSTRAINT IF EXISTS sys_role_dept_pkey")
    op.execute("ALTER TABLE sys_role_menu DROP CONSTRAINT IF EXISTS sys_role_menu_pkey")
    op.execute("ALTER TABLE sys_user_dept DROP CONSTRAINT IF EXISTS sys_user_dept_pkey")
    op.execute("ALTER TABLE sys_user_role DROP CONSTRAINT IF EXISTS sys_user_role_pkey")
    op.execute("ALTER TABLE ai_agent ALTER COLUMN enabled SET DEFAULT false")
    op.execute("ALTER TABLE ai_agent ALTER COLUMN is_builtin SET DEFAULT false")
    op.execute("ALTER TABLE ai_agent ALTER COLUMN display_order SET DEFAULT 0")
    op.execute("ALTER TABLE ai_agent ALTER COLUMN system_prompt SET DEFAULT ''::text")
    op.execute(
        "ALTER TABLE ai_agent ALTER COLUMN risk_appetite SET DEFAULT 'balanced'::character varying"
    )
    op.execute(
        "ALTER TABLE ai_operation_log ALTER COLUMN is_security_event SET DEFAULT false"
    )
    op.execute("ALTER TABLE mk_app ALTER COLUMN tenant_id DROP DEFAULT")
    op.execute("ALTER TABLE mk_tenant_app ALTER COLUMN tenant_id DROP DEFAULT")
    op.execute("ALTER TABLE role_ai_agent ALTER COLUMN enabled SET DEFAULT true")
    op.execute(
        "ALTER TABLE sys_user_export_task ALTER COLUMN status SET DEFAULT 'CREATED'::export_task_status"
    )
    op.execute(
        "ALTER TABLE sys_user_import_batch ALTER COLUMN summary_new SET DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE sys_user_import_batch ALTER COLUMN summary_exists SET DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE sys_user_import_batch ALTER COLUMN summary_conflict SET DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE sys_user_import_batch ALTER COLUMN summary_out_of_scope SET DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE sys_user_import_batch ALTER COLUMN success_count SET DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE sys_user_import_batch ALTER COLUMN skipped_count SET DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE sys_user_import_batch ALTER COLUMN overwritten_count SET DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE sys_user_import_batch ALTER COLUMN failed_count SET DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE sys_user_import_batch ALTER COLUMN status SET DEFAULT 'CREATED'::import_batch_status"
    )
    op.execute(
        "ALTER TABLE ai_agent ADD CONSTRAINT ck_ai_agent_risk_appetite CHECK (((risk_appetite)::text = ANY ((ARRAY['conservative'::character varying, 'balanced'::character varying, 'aggressive'::character varying])::text[])))"
    )
    op.execute(
        "ALTER TABLE ai_message ADD CONSTRAINT ck_ai_message_routing_feedback CHECK (((routing_feedback IS NULL) OR ((routing_feedback)::text = ANY ((ARRAY['correct'::character varying, 'wrong'::character varying])::text[]))))"
    )
    op.execute(
        "ALTER TABLE sys_login_log ADD CONSTRAINT ck_sys_login_log_audit_scope CHECK (((((audit_scope)::text = 'tenant'::text) AND (tenant_id IS NOT NULL)) OR (((audit_scope)::text = 'unresolved'::text) AND (tenant_id IS NULL)) OR ((audit_scope)::text = 'platform'::text)))"
    )
    op.execute(
        "ALTER TABLE sys_operation_log ADD CONSTRAINT ck_sys_operation_log_audit_scope CHECK (((audit_scope)::text = ANY ((ARRAY['tenant'::character varying, 'platform'::character varying])::text[])))"
    )
    op.execute("ALTER TABLE sys_role_dept DROP CONSTRAINT IF EXISTS sys_role_dept_pkey")
    op.execute(
        "ALTER TABLE sys_role_dept ADD PRIMARY KEY (tenant_id, role_id, dept_id)"
    )
    op.execute("ALTER TABLE sys_role_menu DROP CONSTRAINT IF EXISTS sys_role_menu_pkey")
    op.execute(
        "ALTER TABLE sys_role_menu ADD PRIMARY KEY (tenant_id, role_id, menu_id)"
    )
    op.execute(
        "ALTER TABLE sys_user ADD CONSTRAINT ck_sys_user_auth_version_positive CHECK ((auth_version >= 1))"
    )
    op.execute("ALTER TABLE sys_user_dept DROP CONSTRAINT IF EXISTS sys_user_dept_pkey")
    op.execute(
        "ALTER TABLE sys_user_dept ADD PRIMARY KEY (tenant_id, user_id, dept_id)"
    )
    op.execute("ALTER TABLE sys_user_role DROP CONSTRAINT IF EXISTS sys_user_role_pkey")
    op.execute(
        "ALTER TABLE sys_user_role ADD PRIMARY KEY (tenant_id, user_id, role_id)"
    )

    # Preserve known login ownership; unresolved failures remain unscoped.
    op.execute(
        sa.text(
            "UPDATE sys_login_log AS target SET tenant_id = actor.tenant_id, "
            "audit_scope = 'tenant' FROM sys_user AS actor "
            "WHERE actor.user_id = target.user_id"
        )
    )
    op.execute(
        sa.text(
            "UPDATE sys_login_log SET tenant_id = 0, audit_scope = 'tenant' "
            "WHERE audit_scope = 'unresolved' AND user_id IS NOT NULL AND status = '1'"
        )
    )

    # Preserve the existing default tenant's enabled models during the upgrade.
    op.execute(
        sa.text(
            "INSERT INTO tenant_ai_model_policy (tenant_id, model_id, enabled, is_default) "
            "SELECT 0, model_id, true, "
            "row_number() OVER (ORDER BY sort_order, model_id) = 1 "
            "FROM ai_model WHERE is_enabled = true"
        )
    )

    # Schema autogeneration does not capture database security triggers.
    op.execute(_CREATE_APPEND_ONLY_FUNCTION)
    op.execute(_CREATE_APPEND_ONLY_TRIGGER)
    op.execute(_CREATE_PRINCIPAL_VERSION_FUNCTION)
    op.execute(_CREATE_PRINCIPAL_VERSION_TRIGGER)
    op.execute(_CREATE_LINEAGE_FUNCTION)
    op.execute(_CREATE_LINEAGE_TRIGGER)
    op.execute(_CREATE_TENANT_VERSION_FUNCTION)
    op.execute(_CREATE_TENANT_VERSION_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_platform_audit_append_only ON sys_platform_audit_log")
    op.execute(
        "DROP TRIGGER trg_platform_audit_validate_lineage ON sys_platform_audit_log"
    )
    op.execute(
        "DROP TRIGGER trg_platform_principal_security_version ON sys_platform_principal"
    )
    op.execute("DROP TRIGGER trg_sys_tenant_security_version ON sys_tenant")
    op.execute("DROP FUNCTION reject_platform_audit_mutation()")
    op.execute("DROP FUNCTION validate_platform_audit_lineage()")
    op.execute("DROP FUNCTION bump_platform_principal_security_version()")
    op.execute("DROP FUNCTION bump_sys_tenant_security_version()")

    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.execute(
        "ALTER TABLE sys_user_role DROP CONSTRAINT IF EXISTS fk_sys_user_role_tenant_user_id"
    )
    op.execute(
        "ALTER TABLE sys_user_role DROP CONSTRAINT IF EXISTS fk_sys_user_role_tenant_role_id"
    )
    op.create_foreign_key(
        op.f("sys_user_role_user_id_fkey"),
        "sys_user_role",
        "sys_user",
        ["user_id"],
        ["user_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("sys_user_role_role_id_fkey"),
        "sys_user_role",
        "sys_role",
        ["role_id"],
        ["role_id"],
        ondelete="CASCADE",
    )
    op.drop_column("sys_user_role", "tenant_id")
    op.execute(
        "ALTER TABLE sys_user_dept DROP CONSTRAINT IF EXISTS fk_sys_user_dept_tenant_user_id"
    )
    op.execute(
        "ALTER TABLE sys_user_dept DROP CONSTRAINT IF EXISTS fk_sys_user_dept_tenant_dept_id"
    )
    op.create_foreign_key(
        op.f("sys_user_dept_user_id_fkey"),
        "sys_user_dept",
        "sys_user",
        ["user_id"],
        ["user_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("sys_user_dept_dept_id_fkey"),
        "sys_user_dept",
        "sys_dept",
        ["dept_id"],
        ["dept_id"],
        ondelete="CASCADE",
    )
    op.drop_column("sys_user_dept", "tenant_id")
    op.execute(
        "ALTER TABLE sys_user DROP CONSTRAINT IF EXISTS fk_sys_user_tenant_id_sys_tenant"
    )
    op.execute(
        "ALTER TABLE sys_user DROP CONSTRAINT IF EXISTS uq_sys_user_tenant_user_name CASCADE"
    )
    op.execute(
        "ALTER TABLE sys_user DROP CONSTRAINT IF EXISTS uq_sys_user_tenant_user_id CASCADE"
    )
    op.execute(
        "ALTER TABLE sys_user DROP CONSTRAINT IF EXISTS uq_sys_user_tenant_employee_no CASCADE"
    )
    op.create_index(
        op.f("ix_sys_user_user_name"), "sys_user", ["user_name"], unique=True
    )
    op.alter_column(
        "sys_user",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_user",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_user",
        "user_gender",
        existing_type=sa.VARCHAR(length=1),
        comment=None,
        existing_comment="用户性别: 0:未知,1:男,2:女",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_user",
        "user_phone",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="手机号",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_user",
        "user_email",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="邮箱",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_user",
        "user_avatar",
        existing_type=sa.VARCHAR(length=255),
        comment=None,
        existing_comment="头像地址",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_user",
        "status",
        existing_type=sa.VARCHAR(length=10),
        comment=None,
        existing_comment="状态",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_user",
        "hashed_password",
        existing_type=sa.VARCHAR(length=255),
        comment=None,
        existing_comment="加密密码",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_user",
        "nickname",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="昵称",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_user",
        "user_name",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="账号",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_user",
        "user_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="用户ID",
        existing_nullable=False,
    )
    op.drop_column("sys_user", "auth_version")
    op.drop_column("sys_user", "employee_no")
    op.drop_column("sys_user", "tenant_id")
    op.execute(
        "ALTER TABLE sys_role_menu DROP CONSTRAINT IF EXISTS fk_sys_role_menu_tenant_role_id"
    )
    op.execute(
        "ALTER TABLE sys_role_menu DROP CONSTRAINT IF EXISTS fk_sys_role_menu_tenant_menu_id"
    )
    op.create_foreign_key(
        op.f("sys_role_menu_role_id_fkey"),
        "sys_role_menu",
        "sys_role",
        ["role_id"],
        ["role_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("sys_role_menu_menu_id_fkey"),
        "sys_role_menu",
        "sys_menu",
        ["menu_id"],
        ["menu_id"],
        ondelete="CASCADE",
    )
    op.drop_column("sys_role_menu", "tenant_id")
    op.execute(
        "ALTER TABLE sys_role_dept DROP CONSTRAINT IF EXISTS fk_sys_role_dept_tenant_role_id"
    )
    op.execute(
        "ALTER TABLE sys_role_dept DROP CONSTRAINT IF EXISTS fk_sys_role_dept_tenant_dept_id"
    )
    op.create_foreign_key(
        op.f("sys_role_dept_dept_id_fkey"),
        "sys_role_dept",
        "sys_dept",
        ["dept_id"],
        ["dept_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("sys_role_dept_role_id_fkey"),
        "sys_role_dept",
        "sys_role",
        ["role_id"],
        ["role_id"],
        ondelete="CASCADE",
    )
    op.drop_column("sys_role_dept", "tenant_id")
    op.execute(
        "ALTER TABLE sys_role DROP CONSTRAINT IF EXISTS fk_sys_role_tenant_id_sys_tenant"
    )
    op.execute(
        "ALTER TABLE sys_role DROP CONSTRAINT IF EXISTS uq_sys_role_tenant_role_name CASCADE"
    )
    op.execute(
        "ALTER TABLE sys_role DROP CONSTRAINT IF EXISTS uq_sys_role_tenant_role_id CASCADE"
    )
    op.execute(
        "ALTER TABLE sys_role DROP CONSTRAINT IF EXISTS uq_sys_role_tenant_role_code CASCADE"
    )
    op.create_unique_constraint(
        op.f("sys_role_role_name_key"),
        "sys_role",
        ["role_name"],
        postgresql_nulls_not_distinct=False,
    )
    op.create_unique_constraint(
        op.f("sys_role_role_code_key"),
        "sys_role",
        ["role_code"],
        postgresql_nulls_not_distinct=False,
    )
    op.alter_column(
        "sys_role",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_role",
        "update_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="更新人",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_role",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_role",
        "create_by",
        existing_type=sa.VARCHAR(length=32),
        comment=None,
        existing_comment="创建人",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_role",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment=None,
        existing_comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_role",
        "data_scope",
        existing_type=sa.VARCHAR(length=2),
        comment=None,
        existing_comment="数据权限范围：1-全部，2-自定义，3-本部门，4-本部门及以下，5-仅本人",
        existing_nullable=False,
        existing_server_default=sa.text("'1'::character varying"),
    )
    op.alter_column(
        "sys_role",
        "role_desc",
        existing_type=sa.VARCHAR(length=255),
        comment=None,
        existing_comment="角色描述",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_role",
        "role_code",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="角色编码",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_role",
        "role_name",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="角色名称",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_role",
        "role_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="角色ID",
        existing_nullable=False,
    )
    op.drop_column("sys_role", "tenant_id")
    op.execute(
        "ALTER TABLE sys_operation_log DROP CONSTRAINT IF EXISTS fk_sys_operation_log_tenant_id_sys_tenant"
    )
    op.execute(
        "ALTER TABLE sys_operation_log DROP CONSTRAINT IF EXISTS uq_sys_operation_log_tenant_log_id CASCADE"
    )
    op.create_index(
        op.f("ix_operation_log_user_id"), "sys_operation_log", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_operation_log_create_time"),
        "sys_operation_log",
        ["create_time"],
        unique=False,
    )
    op.alter_column(
        "sys_operation_log",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="操作时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_operation_log",
        "duration",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="耗时（毫秒）",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_operation_log",
        "ip",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="操作者IP",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_operation_log",
        "status_code",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="响应状态码",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_operation_log",
        "request_params",
        existing_type=sa.TEXT(),
        comment=None,
        existing_comment="请求参数摘要",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_operation_log",
        "path",
        existing_type=sa.VARCHAR(length=200),
        comment=None,
        existing_comment="请求路径",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_operation_log",
        "method",
        existing_type=sa.VARCHAR(length=10),
        comment=None,
        existing_comment="HTTP方法",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_operation_log",
        "action",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="操作类型",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_operation_log",
        "module",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="业务模块",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_operation_log",
        "username",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="操作人用户名",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_operation_log",
        "user_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="操作人ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_operation_log",
        "operation_log_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="日志ID",
        existing_nullable=False,
    )
    op.drop_column("sys_operation_log", "audit_scope")
    op.drop_column("sys_operation_log", "tenant_id")
    op.execute(
        "ALTER TABLE sys_menu DROP CONSTRAINT IF EXISTS fk_sys_menu_tenant_parent"
    )
    op.execute(
        "ALTER TABLE sys_menu DROP CONSTRAINT IF EXISTS fk_sys_menu_tenant_id_sys_tenant"
    )
    op.execute(
        "ALTER TABLE sys_menu DROP CONSTRAINT IF EXISTS uq_sys_menu_tenant_menu_id CASCADE"
    )
    op.alter_column(
        "sys_menu",
        "query",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        comment=None,
        existing_comment="路由参数",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "permission",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="按钮/功能权限",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "fixed_index_in_tab",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="页签固定索引",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "multi_tab",
        existing_type=sa.BOOLEAN(),
        comment=None,
        existing_comment="是否支持多页签",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "active_menu",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="激活菜单的路由名称",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "hide_in_menu",
        existing_type=sa.BOOLEAN(),
        comment=None,
        existing_comment="隐藏菜单",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "href",
        existing_type=sa.VARCHAR(length=255),
        comment=None,
        existing_comment="外链",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "constant",
        existing_type=sa.BOOLEAN(),
        comment=None,
        existing_comment="常量路由",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "keep_alive",
        existing_type=sa.BOOLEAN(),
        comment=None,
        existing_comment="缓存路由",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "i18n_key",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="国际化key",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "layout",
        existing_type=sa.VARCHAR(length=255),
        comment=None,
        existing_comment="布局组件",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "page",
        existing_type=sa.VARCHAR(length=255),
        comment=None,
        existing_comment="页面组件",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "path_param",
        existing_type=sa.VARCHAR(length=255),
        comment=None,
        existing_comment="路径参数",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_menu",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_menu",
        "update_by",
        existing_type=sa.VARCHAR(length=32),
        comment=None,
        existing_comment="更新人",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "create_by",
        existing_type=sa.VARCHAR(length=32),
        comment=None,
        existing_comment="创建人",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment=None,
        existing_comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_menu",
        "order",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="排序（越小越靠前）",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_menu",
        "route_path",
        existing_type=sa.VARCHAR(length=255),
        comment=None,
        existing_comment="前端路由路径",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "route_name",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="前端路由名称（name）",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "component",
        existing_type=sa.VARCHAR(length=255),
        comment=None,
        existing_comment="组件",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "path",
        existing_type=sa.VARCHAR(length=255),
        comment=None,
        existing_comment="路由路径",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "icon_type",
        existing_type=sa.VARCHAR(length=1),
        comment=None,
        existing_comment="菜单图标类型",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "icon",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="菜单图标",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "menu_type",
        existing_type=sa.VARCHAR(length=1),
        comment=None,
        existing_comment="类型: M目录, C菜单, F按钮",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_menu",
        "menu_name",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="菜单标题",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_menu",
        "parent_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="父菜单ID",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_menu",
        "menu_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="菜单ID",
        existing_nullable=False,
    )
    op.drop_column("sys_menu", "tenant_id")
    op.execute(
        "ALTER TABLE sys_login_log DROP CONSTRAINT IF EXISTS fk_sys_login_log_tenant_id_sys_tenant"
    )
    op.execute(
        "ALTER TABLE sys_login_log DROP CONSTRAINT IF EXISTS uq_sys_login_log_tenant_log_id CASCADE"
    )
    op.create_index(
        op.f("ix_login_log_login_time"), "sys_login_log", ["login_time"], unique=False
    )
    op.alter_column(
        "sys_login_log",
        "login_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="登录时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_login_log",
        "message",
        existing_type=sa.VARCHAR(length=200),
        comment=None,
        existing_comment="结果描述",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_login_log",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment=None,
        existing_comment="状态：1-成功，2-失败，3-锁定",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_login_log",
        "user_agent",
        existing_type=sa.VARCHAR(length=500),
        comment=None,
        existing_comment="浏览器信息",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_login_log",
        "ip",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="登录IP",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_login_log",
        "username",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="登录用户名",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_login_log",
        "user_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="登录用户ID",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_login_log",
        "login_log_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="日志ID",
        existing_nullable=False,
    )
    op.drop_column("sys_login_log", "audit_scope")
    op.drop_column("sys_login_log", "tenant_id")
    op.execute(
        "ALTER TABLE sys_job_log DROP CONSTRAINT IF EXISTS fk_sys_job_log_tenant_job"
    )
    op.execute(
        "ALTER TABLE sys_job_log DROP CONSTRAINT IF EXISTS fk_sys_job_log_tenant_job"
    )
    op.execute(
        "ALTER TABLE sys_job_log DROP CONSTRAINT IF EXISTS uq_sys_job_log_tenant_log_id CASCADE"
    )
    op.alter_column(
        "sys_job_log",
        "attempt_count",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="本次触发实际执行次数（含重试）",
        existing_nullable=False,
        existing_server_default=sa.text("1"),
    )
    op.alter_column(
        "sys_job_log",
        "duration",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="耗时（毫秒）",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job_log",
        "end_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="结束时间",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job_log",
        "start_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="开始时间",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job_log",
        "error_msg",
        existing_type=sa.TEXT(),
        comment=None,
        existing_comment="异常信息",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job_log",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment=None,
        existing_comment="状态：1-成功，2-失败，3-执行中",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job_log",
        "job_key",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="任务标识",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job_log",
        "job_name",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="任务名称",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job_log",
        "job_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="任务ID",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job_log",
        "job_log_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="日志ID",
        existing_nullable=False,
    )
    op.drop_column("sys_job_log", "runner_id")
    op.drop_column("sys_job_log", "tenant_id")
    op.execute(
        "ALTER TABLE sys_job DROP CONSTRAINT IF EXISTS fk_sys_job_tenant_id_sys_tenant"
    )
    op.execute(
        "ALTER TABLE sys_job DROP CONSTRAINT IF EXISTS uq_sys_job_tenant_job_key CASCADE"
    )
    op.execute(
        "ALTER TABLE sys_job DROP CONSTRAINT IF EXISTS uq_sys_job_tenant_job_id CASCADE"
    )
    op.create_unique_constraint(
        op.f("sys_job_job_key_key"),
        "sys_job",
        ["job_key"],
        postgresql_nulls_not_distinct=False,
    )
    op.alter_column(
        "sys_job",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_job",
        "update_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="更新者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_job",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "remark",
        existing_type=sa.VARCHAR(length=256),
        comment=None,
        existing_comment="备注",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "run_on_enable",
        existing_type=sa.BOOLEAN(),
        comment=None,
        existing_comment="启用时是否立即执行一次",
        existing_nullable=False,
        existing_server_default=sa.text("false"),
    )
    op.alter_column(
        "sys_job",
        "max_retries",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="失败重试次数（0 表示不重试）",
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "sys_job",
        "timeout_seconds",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="单次执行超时秒数（空表示不限）",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "concurrent",
        existing_type=sa.VARCHAR(length=2),
        comment=None,
        existing_comment="并发策略：1-允许，2-不允许",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment=None,
        existing_comment="状态：1-启用，2-停用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job",
        "job_args",
        existing_type=sa.TEXT(),
        comment=None,
        existing_comment="任务参数JSON",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "interval_unit",
        existing_type=sa.VARCHAR(length=10),
        comment=None,
        existing_comment="间隔单位：seconds/minutes/hours/days",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "interval_value",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="间隔值",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "trigger_type",
        existing_type=sa.VARCHAR(length=10),
        comment=None,
        existing_comment="调度类型：cron-表达式，interval-间隔",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job",
        "cron_expression",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="cron表达式",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_job",
        "job_key",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="任务标识",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job",
        "job_name",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="任务名称",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_job",
        "job_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="任务ID",
        existing_nullable=False,
    )
    op.drop_column("sys_job", "tenant_id")
    op.execute(
        "ALTER TABLE sys_file DROP CONSTRAINT IF EXISTS fk_sys_file_tenant_owner"
    )
    op.execute(
        "ALTER TABLE sys_file DROP CONSTRAINT IF EXISTS fk_sys_file_tenant_owner"
    )
    op.execute(
        "ALTER TABLE sys_file DROP CONSTRAINT IF EXISTS uq_sys_file_tenant_file_id CASCADE"
    )
    op.alter_column(
        "sys_file",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="上传时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_file",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="上传者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_file",
        "del_flag",
        existing_type=sa.VARCHAR(length=1),
        comment=None,
        existing_comment="删除标记: 0-正常, 1-已删除",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "business_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="业务记录ID",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_file",
        "business_type",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="业务类型(如product、avatar)",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_file",
        "mime_type",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="MIME类型",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_file",
        "file_ext",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="文件扩展名",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "file_size",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="文件大小(字节)",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "file_url",
        existing_type=sa.VARCHAR(length=500),
        comment=None,
        existing_comment="文件访问URL",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "file_path",
        existing_type=sa.VARCHAR(length=500),
        comment=None,
        existing_comment="相对路径",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "file_name",
        existing_type=sa.VARCHAR(length=255),
        comment=None,
        existing_comment="存储文件名(Snowflake ID)",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "original_name",
        existing_type=sa.VARCHAR(length=255),
        comment=None,
        existing_comment="原始文件名",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_file",
        "file_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="文件ID",
        existing_nullable=False,
    )
    op.drop_column("sys_file", "tenant_id")
    op.drop_column("sys_file", "owner_user_id")
    op.execute(
        "ALTER TABLE sys_dict_type DROP CONSTRAINT IF EXISTS fk_sys_dict_type_tenant_id_sys_tenant"
    )
    op.execute(
        "ALTER TABLE sys_dict_type DROP CONSTRAINT IF EXISTS uq_sys_dict_type_tenant_type_id CASCADE"
    )
    op.execute(
        "ALTER TABLE sys_dict_type DROP CONSTRAINT IF EXISTS uq_sys_dict_type_tenant_type CASCADE"
    )
    op.execute(
        "ALTER TABLE sys_dict_type DROP CONSTRAINT IF EXISTS uq_sys_dict_type_tenant_name CASCADE"
    )
    op.create_unique_constraint(
        op.f("sys_dict_type_dict_type_key"),
        "sys_dict_type",
        ["dict_type"],
        postgresql_nulls_not_distinct=False,
    )
    op.create_unique_constraint(
        op.f("sys_dict_type_dict_name_key"),
        "sys_dict_type",
        ["dict_name"],
        postgresql_nulls_not_distinct=False,
    )
    op.alter_column(
        "sys_dict_type",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_dict_type",
        "update_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="更新者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_type",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_dict_type",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_type",
        "remark",
        existing_type=sa.VARCHAR(length=500),
        comment=None,
        existing_comment="备注",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_type",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment=None,
        existing_comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_type",
        "dict_type",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="字典类型",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_type",
        "dict_name",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="字典名称",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_type",
        "dict_type_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="字典类型ID",
        existing_nullable=False,
    )
    op.drop_column("sys_dict_type", "tenant_id")
    op.execute(
        "ALTER TABLE sys_dict_data DROP CONSTRAINT IF EXISTS fk_sys_dict_data_tenant_type"
    )
    op.execute(
        "ALTER TABLE sys_dict_data DROP CONSTRAINT IF EXISTS fk_sys_dict_data_tenant_type"
    )
    op.execute(
        "ALTER TABLE sys_dict_data DROP CONSTRAINT IF EXISTS uq_sys_dict_data_tenant_code CASCADE"
    )
    op.alter_column(
        "sys_dict_data",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_dict_data",
        "update_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="更新者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_data",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_dict_data",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_data",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment=None,
        existing_comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_data",
        "is_default",
        existing_type=sa.VARCHAR(length=2),
        comment=None,
        existing_comment="是否默认：Y-是，N-否",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_data",
        "list_class",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="表格回显样式",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_data",
        "css_class",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="样式属性",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dict_data",
        "dict_type",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="字典类型",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_data",
        "dict_value",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="字典键值",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_data",
        "dict_label",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="字典标签",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_data",
        "dict_sort",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="字典排序",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dict_data",
        "dict_code",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="字典编码",
        existing_nullable=False,
    )
    op.drop_column("sys_dict_data", "tenant_id")
    op.execute(
        "ALTER TABLE sys_dept DROP CONSTRAINT IF EXISTS fk_sys_dept_tenant_parent"
    )
    op.execute(
        "ALTER TABLE sys_dept DROP CONSTRAINT IF EXISTS fk_sys_dept_tenant_parent"
    )
    op.execute(
        "ALTER TABLE sys_dept DROP CONSTRAINT IF EXISTS uq_sys_dept_tenant_dept_id CASCADE"
    )
    op.alter_column(
        "sys_dept",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_dept",
        "update_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="更新者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_dept",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment=None,
        existing_comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dept",
        "email",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="邮箱",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "phone",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="联系电话",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "leader",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="负责人",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "order_num",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="显示顺序",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dept",
        "dept_name",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="部门名称",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_dept",
        "ancestors",
        existing_type=sa.VARCHAR(length=500),
        comment=None,
        existing_comment="祖先路径",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "parent_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="父部门ID",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_dept",
        "dept_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="部门ID",
        existing_nullable=False,
    )
    op.drop_column("sys_dept", "tenant_id")
    op.execute(
        "ALTER TABLE sys_data_scope_demo DROP CONSTRAINT IF EXISTS fk_sys_data_scope_demo_tenant_creator"
    )
    op.execute(
        "ALTER TABLE sys_data_scope_demo DROP CONSTRAINT IF EXISTS fk_sys_data_scope_demo_tenant_dept"
    )
    op.execute(
        "ALTER TABLE sys_data_scope_demo DROP CONSTRAINT IF EXISTS fk_sys_data_scope_demo_tenant_dept"
    )
    op.execute(
        "ALTER TABLE sys_data_scope_demo DROP CONSTRAINT IF EXISTS uq_sys_data_scope_demo_tenant_demo_id CASCADE"
    )
    op.alter_column(
        "sys_data_scope_demo",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_data_scope_demo",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_data_scope_demo",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment=None,
        existing_comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_data_scope_demo",
        "create_by",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="创建人 user_id（SELF scope 锚点，存 ID 而非 user_name）",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_data_scope_demo",
        "dept_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="所属部门ID（数据权限锚点）",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_data_scope_demo",
        "content",
        existing_type=sa.TEXT(),
        comment=None,
        existing_comment="内容",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_data_scope_demo",
        "title",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="标题",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_data_scope_demo",
        "demo_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="演示数据ID",
        existing_nullable=False,
    )
    op.drop_column("sys_data_scope_demo", "tenant_id")
    op.execute(
        "ALTER TABLE sys_config DROP CONSTRAINT IF EXISTS fk_sys_config_tenant_id_sys_tenant"
    )
    op.execute(
        "ALTER TABLE sys_config DROP CONSTRAINT IF EXISTS uq_sys_config_tenant_config_key CASCADE"
    )
    op.execute(
        "ALTER TABLE sys_config DROP CONSTRAINT IF EXISTS uq_sys_config_tenant_config_id CASCADE"
    )
    op.create_unique_constraint(
        op.f("sys_config_config_key_key"),
        "sys_config",
        ["config_key"],
        postgresql_nulls_not_distinct=False,
    )
    op.alter_column(
        "sys_config",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_config",
        "update_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="更新者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_config",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "sys_config",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_config",
        "remark",
        existing_type=sa.VARCHAR(length=500),
        comment=None,
        existing_comment="备注",
        existing_nullable=True,
    )
    op.alter_column(
        "sys_config",
        "is_public",
        existing_type=sa.BOOLEAN(),
        comment=None,
        existing_comment="是否公开访问",
        existing_nullable=False,
        existing_server_default=sa.text("false"),
    )
    op.alter_column(
        "sys_config",
        "status",
        existing_type=sa.VARCHAR(length=2),
        comment=None,
        existing_comment="状态：1-启用，2-禁用",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_config",
        "config_group",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="配置分组",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_config",
        "config_type",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="配置类型：text/richtext/file",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_config",
        "config_value",
        existing_type=sa.TEXT(),
        comment=None,
        existing_comment="配置值",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_config",
        "config_key",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="配置键",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_config",
        "config_name",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="配置名称",
        existing_nullable=False,
    )
    op.alter_column(
        "sys_config",
        "config_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="配置ID",
        existing_nullable=False,
    )
    op.drop_column("sys_config", "tenant_id")
    op.alter_column(
        "mk_tenant_app",
        "updated_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_tenant_app",
        "installed_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment=None,
        existing_comment="首次安装时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_tenant_app",
        "has_data",
        existing_type=sa.BOOLEAN(),
        comment=None,
        existing_comment="是否存在业务数据（决定是否可硬删除）",
        existing_nullable=False,
        existing_server_default=sa.text("false"),
    )
    op.alter_column(
        "mk_tenant_app",
        "retained_table_names",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment=None,
        existing_comment="卸载后保留的数据表名清单",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_tenant_app",
        "approved_permissions",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment=None,
        existing_comment="用户已批准的权限清单",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_tenant_app",
        "config",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment=None,
        existing_comment="安装配置（用户填写的）",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_tenant_app",
        "status",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="安装状态: installed|disabled|uninstalled",
        existing_nullable=False,
        existing_server_default=sa.text("'installed'::character varying"),
    )
    op.alter_column(
        "mk_tenant_app",
        "installed_version",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="已安装版本号（semver 字符串）",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_tenant_app",
        "app_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="已安装应用ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_tenant_app",
        "tenant_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="租户 ID；必须由可信 TenantContext 显式写入",
        existing_nullable=False,
        existing_server_default=sa.text("'0'::bigint"),
    )
    op.alter_column(
        "mk_tenant_app",
        "id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="安装记录ID",
        existing_nullable=False,
    )
    op.drop_table_comment(
        "mk_app_version", existing_comment="应用版本（每次发布一行）", schema=None
    )
    op.alter_column(
        "mk_app_version",
        "created_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app_version",
        "review_status",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="审核状态: pending|approved|rejected",
        existing_nullable=False,
        existing_server_default=sa.text("'pending'::character varying"),
    )
    op.alter_column(
        "mk_app_version",
        "file_size",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="制品包大小（字节）",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_version",
        "file_hash",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="制品包 SHA-256",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_version",
        "file_url",
        existing_type=sa.VARCHAR(length=500),
        comment=None,
        existing_comment="制品包 URL",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_version",
        "manifest",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment=None,
        existing_comment="应用清单（JSON）",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_version",
        "changelog",
        existing_type=sa.TEXT(),
        comment=None,
        existing_comment="变更说明",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_version",
        "version",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="语义化版本号（semver）",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_version",
        "app_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="所属应用ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_version",
        "id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="版本ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_review",
        "updated_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app_review",
        "created_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app_review",
        "final_status",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="最终审核状态: pending|approved|rejected",
        existing_nullable=False,
        existing_server_default=sa.text("'pending'::character varying"),
    )
    op.alter_column(
        "mk_app_review",
        "human_reviewed_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment=None,
        existing_comment="人工审核完成时间",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "human_comment",
        existing_type=sa.TEXT(),
        comment=None,
        existing_comment="人工审核意见",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "human_reviewer_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="人工审核人ID",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "human_status",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="人工审核状态: pending|approved|rejected",
        existing_nullable=False,
        existing_server_default=sa.text("'pending'::character varying"),
    )
    op.alter_column(
        "mk_app_review",
        "ai_review_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment=None,
        existing_comment="AI 审核完成时间",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "ai_report",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment=None,
        existing_comment="AI 审核报告",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "ai_risk_level",
        existing_type=sa.VARCHAR(length=10),
        comment=None,
        existing_comment="AI 风险等级: low|medium|high",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "rule_check_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment=None,
        existing_comment="规则检查完成时间",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "rule_check_result",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment=None,
        existing_comment="规则引擎检查结果",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_review",
        "version_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="被审核版本ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_review",
        "app_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="所属应用ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_review",
        "id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="审核记录ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_rating",
        "updated_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app_rating",
        "created_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app_rating",
        "comment",
        existing_type=sa.TEXT(),
        comment=None,
        existing_comment="评分评论",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app_rating",
        "rating",
        existing_type=sa.SMALLINT(),
        comment=None,
        existing_comment="评分（1-5）",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_rating",
        "user_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="评分用户ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_rating",
        "app_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="被评分应用ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_rating",
        "id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="评分ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_permission",
        "created_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app_permission",
        "detail_canonical",
        existing_type=sa.TEXT(),
        comment=None,
        existing_comment="审计字段：canonical JSON 文本，便于 hash 算法迁移",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_permission",
        "detail_hash",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="detail canonical JSON 的 SHA-256",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_permission",
        "detail",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment=None,
        existing_comment="权限详情（原始结构）",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_permission",
        "type",
        existing_type=sa.VARCHAR(length=30),
        comment=None,
        existing_comment="权限类型: api|external_api|menu|db_table|...",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_permission",
        "app_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="所属应用ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app_permission",
        "id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="权限ID",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app",
        "updated_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app",
        "created_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "mk_app",
        "tags_text",
        existing_type=sa.TEXT(),
        comment=None,
        existing_comment="冗余：mk_app_tag 名称以空格拼接，用于 search_vector",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "rating_count",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="评分人数",
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "mk_app",
        "avg_rating",
        existing_type=sa.NUMERIC(precision=3, scale=1),
        comment=None,
        existing_comment="平均评分（0-5）",
        existing_nullable=False,
        existing_server_default=sa.text("0.0"),
    )
    op.alter_column(
        "mk_app",
        "download_count",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="累计下载次数",
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "mk_app",
        "license",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="开源协议",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "homepage",
        existing_type=sa.VARCHAR(length=500),
        comment=None,
        existing_comment="主页 URL",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "current_version_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="当前发布版本ID",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "status",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="状态: draft|published|...",
        existing_nullable=False,
        existing_server_default=sa.text("'draft'::character varying"),
    )
    op.alter_column(
        "mk_app",
        "author_name",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="作者名（冗余展示字段）",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "author_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="作者用户ID",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "icon",
        existing_type=sa.VARCHAR(length=500),
        comment=None,
        existing_comment="图标 URL（对象存储，禁止外链）",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "description",
        existing_type=sa.TEXT(),
        comment=None,
        existing_comment="应用描述",
        existing_nullable=True,
    )
    op.alter_column(
        "mk_app",
        "category",
        existing_type=sa.VARCHAR(length=30),
        comment=None,
        existing_comment="应用分类",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app",
        "type",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="应用类型: lowcode|frontend|backend|fullstack|theme|bundle",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app",
        "slug",
        existing_type=sa.VARCHAR(length=150),
        comment=None,
        existing_comment="URL slug（唯一）",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app",
        "name",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="应用名称",
        existing_nullable=False,
    )
    op.alter_column(
        "mk_app",
        "tenant_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="租户 ID；必须由可信 TenantContext 显式写入",
        existing_nullable=False,
        existing_server_default=sa.text("'0'::bigint"),
    )
    op.alter_column(
        "mk_app",
        "id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="应用ID",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_provider",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "ai_provider",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "ai_provider",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_provider",
        "config",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        comment=None,
        existing_comment="扩展配置",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_provider",
        "is_enabled",
        existing_type=sa.BOOLEAN(),
        comment=None,
        existing_comment="是否启用",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_provider",
        "base_url",
        existing_type=sa.VARCHAR(length=500),
        comment=None,
        existing_comment="默认 API 地址",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_provider",
        "api_key",
        existing_type=sa.VARCHAR(length=500),
        comment=None,
        existing_comment="API Key（加密存储）",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_provider",
        "name",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="显示名称",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_provider",
        "provider_code",
        existing_type=sa.VARCHAR(length=50),
        comment=None,
        existing_comment="提供商标识：openai / anthropic / deepseek",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_provider",
        "provider_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="提供商ID",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_model",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        nullable=True,
        comment=None,
        existing_comment="更新时间",
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "ai_model",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        nullable=True,
        comment=None,
        existing_comment="创建时间",
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "ai_model",
        "create_by",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment="创建者",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_model",
        "config",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment=None,
        existing_comment="扩展配置",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_model",
        "sort_order",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="排序（越小越靠前）",
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "ai_model",
        "is_enabled",
        existing_type=sa.BOOLEAN(),
        comment=None,
        existing_comment="是否启用",
        existing_nullable=False,
        existing_server_default=sa.text("true"),
    )
    op.alter_column(
        "ai_model",
        "base_url",
        existing_type=sa.VARCHAR(length=500),
        comment=None,
        existing_comment="模型级 API 地址（覆盖提供商默认）",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_model",
        "capabilities",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment=None,
        existing_comment='能力标签，如 ["text","vision","image-gen"]',
        existing_nullable=False,
    )
    op.alter_column(
        "ai_model",
        "name",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="模型名称",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_model",
        "provider_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="所属提供商ID",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_model",
        "model_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="模型ID",
        existing_nullable=False,
    )
    op.execute(
        "ALTER TABLE ai_message DROP CONSTRAINT IF EXISTS fk_ai_message_tenant_conversation"
    )
    op.create_foreign_key(
        op.f("ai_message_conversation_id_fkey"),
        "ai_message",
        "ai_conversation",
        ["conversation_id"],
        ["conversation_id"],
        ondelete="CASCADE",
    )
    op.execute(
        "ALTER TABLE ai_message DROP CONSTRAINT IF EXISTS uq_ai_message_tenant_message_id CASCADE"
    )
    op.alter_column(
        "ai_message",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "ai_message",
        "tool_calls",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        comment=None,
        existing_comment="工具调用记录列表（名称、参数、结果）",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_message",
        "parts",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        comment=None,
        existing_comment="结构化消息内容（含图片、文件等）",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_message",
        "tokens_output",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="输出 token 数",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_message",
        "tokens_input",
        existing_type=sa.INTEGER(),
        comment=None,
        existing_comment="输入 token 数",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_message",
        "content",
        existing_type=sa.TEXT(),
        comment=None,
        existing_comment="消息内容",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_message",
        "message_type",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="类型：text / tool_call / tool_result",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_message",
        "role",
        existing_type=sa.VARCHAR(length=20),
        comment=None,
        existing_comment="角色：user / assistant / system / tool",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_message",
        "parent_message_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="父消息（工具调用关联链）",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_message",
        "conversation_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="所属会话",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_message",
        "message_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="消息ID",
        existing_nullable=False,
    )
    op.drop_column("ai_message", "routing_feedback")
    op.drop_column("ai_message", "projection_dependency_message_ids")
    op.drop_column("ai_message", "resolver_version")
    op.drop_column("ai_message", "data_scope_hash")
    op.drop_column("ai_message", "subject_refs_hash")
    op.drop_column("ai_message", "subject_refs")
    op.drop_column("ai_message", "tool_codes")
    op.drop_column("ai_message", "tenant_id")
    op.drop_column("ai_message", "agent_code")
    op.drop_column("ai_message", "supersedes_message_id")
    op.drop_column("ai_message", "is_active")
    op.drop_column("ai_message", "trace_id")
    op.execute(
        "ALTER TABLE ai_conversation DROP CONSTRAINT IF EXISTS fk_ai_conversation_tenant_user"
    )
    op.create_foreign_key(
        op.f("ai_conversation_user_id_fkey"),
        "ai_conversation",
        "sys_user",
        ["user_id"],
        ["user_id"],
        ondelete="CASCADE",
    )
    op.execute(
        "ALTER TABLE ai_conversation DROP CONSTRAINT IF EXISTS uq_ai_conversation_tenant_conversation_id CASCADE"
    )
    op.alter_column(
        "ai_conversation",
        "update_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="更新时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "ai_conversation",
        "create_time",
        existing_type=postgresql.TIMESTAMP(),
        comment=None,
        existing_comment="创建时间",
        existing_nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        "ai_conversation",
        "status",
        existing_type=sa.SMALLINT(),
        comment=None,
        existing_comment="状态：0=活跃, 1=归档",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_conversation",
        "system_prompt",
        existing_type=sa.TEXT(),
        comment=None,
        existing_comment="系统提示词（Agent instructions）",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_conversation",
        "model_name",
        existing_type=sa.VARCHAR(length=100),
        comment=None,
        existing_comment="使用的模型标识",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_conversation",
        "title",
        existing_type=sa.VARCHAR(length=200),
        comment=None,
        existing_comment="会话标题",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_conversation",
        "user_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="所属用户",
        existing_nullable=False,
    )
    op.alter_column(
        "ai_conversation",
        "conversation_id",
        existing_type=sa.BIGINT(),
        comment=None,
        existing_comment="会话ID",
        existing_nullable=False,
    )
    op.drop_column("ai_conversation", "deleted_at")
    op.drop_column("ai_conversation", "trace_id")
    op.drop_column("ai_conversation", "agent_code")
    op.drop_column("ai_conversation", "tenant_id")
    op.execute("DROP TABLE IF EXISTS ai_agent CASCADE")
    op.execute("DROP TABLE IF EXISTS ai_operation_log CASCADE")
    op.execute("DROP TABLE IF EXISTS ai_prepared_action CASCADE")
    op.execute("DROP TABLE IF EXISTS ai_routing_feedback CASCADE")
    op.execute("DROP TABLE IF EXISTS ai_routing_log CASCADE")
    op.execute("DROP TABLE IF EXISTS sys_platform_principal CASCADE")
    op.execute("DROP TABLE IF EXISTS sys_tenant CASCADE")
    op.execute("DROP TABLE IF EXISTS sys_platform_audit_log CASCADE")
    op.execute("DROP TABLE IF EXISTS role_ai_agent CASCADE")
    op.execute("DROP TABLE IF EXISTS sys_user_export_task CASCADE")
    op.execute("DROP TABLE IF EXISTS sys_user_import_batch CASCADE")
    op.execute("DROP TABLE IF EXISTS tenant_ai_model_policy CASCADE")
    op.execute("DROP TABLE IF EXISTS sys_user_import_batch_log CASCADE")
    # ### end Alembic commands ###
    op.execute("DROP TYPE IF EXISTS export_task_status")
    op.execute("DROP TYPE IF EXISTS import_batch_status")
