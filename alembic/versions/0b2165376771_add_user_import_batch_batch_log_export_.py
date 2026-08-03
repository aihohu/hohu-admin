"""add user import batch, batch log, export task tables and employee_no

Revision ID: 0b2165376771
Revises: fba0cf4a5e82
Create Date: 2026-08-03 11:48:30.548682

Task 2（spec §2.26 / §2.28 / §2.31 / §2.24）：
- sys_user_import_batch（PostgreSQL ENUM status + CHECK，含 PREVIEW_DONE + reason）
- sys_user_import_batch_log（FK CASCADE，spec §2.28）
- sys_user_export_task（spec §2.31）
- sys_user.employee_no 字段（UNIQUE 索引，spec §2.24）

注：alembic autogenerate 检测到 ai_message / ai_model / ai_operation_log /
role_ai_agent 等表的 comment / index 漂移，这些是历史遗留 ORM ↔ DB schema 漂移，
不在 Task 2 范围，已修剪。需要单独的 migration 处理。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0b2165376771"
down_revision: Union[str, Sequence[str], None] = "fba0cf4a5e82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. PostgreSQL ENUM 类型（spec §2.26 v2.2 P0：原生 ENUM + CHECK 双重保证）
    # 用 raw DDL 避免 sa.Enum().create() 在 asyncpg 下 checkfirst 行为问题
    op.execute(
        "CREATE TYPE import_batch_status AS ENUM "
        "('CREATED', 'PREVIEW_DONE', 'RUNNING', 'SUCCESS', "
        "'PARTIAL_SUCCESS', 'FAILED', 'EXPIRED', 'CANCELLED')"
    )
    op.execute(
        "CREATE TYPE export_task_status AS ENUM "
        "('CREATED', 'RUNNING', 'SUCCESS', 'FAILED', 'EXPIRED')"
    )

    # 引用 type 的 column type；create_type=False 告诉 SQLAlchemy type 已建好
    import_batch_status_type = postgresql.ENUM(
        "CREATED",
        "PREVIEW_DONE",
        "RUNNING",
        "SUCCESS",
        "PARTIAL_SUCCESS",
        "FAILED",
        "EXPIRED",
        "CANCELLED",
        name="import_batch_status",
        create_type=False,
    )
    export_task_status_type = postgresql.ENUM(
        "CREATED",
        "RUNNING",
        "SUCCESS",
        "FAILED",
        "EXPIRED",
        name="export_task_status",
        create_type=False,
    )

    # 2. sys_user_import_batch（spec §3.6 v2.2 P1-2：唯一 aggregate root）
    op.create_table(
        "sys_user_import_batch",
        sa.Column("batch_id", sa.String(length=64), nullable=False, comment="UUID"),
        sa.Column("operator_id", sa.BigInteger(), nullable=False),
        sa.Column("filename", sa.String(length=256), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column(
            "preview_token",
            sa.String(length=64),
            nullable=False,
            comment="execute 时反查 batch 行（spec §2.27 幂等）",
        ),
        sa.Column("summary_new", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_exists", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_conflict", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "summary_out_of_scope", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "overwritten_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
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
            comment="原始上传文件 storage_key（spec §3.9 FileStorage Protocol）",
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
            comment="操作理由（spec §2.30 v2.2 P1-3）：批量操作的业务背景，进入审计链路",
        ),
        sa.Column(
            "status",
            import_batch_status_type,
            nullable=False,
            server_default="CREATED",
            comment="状态机详见 §2.26",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("batch_id"),
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
        op.f("ix_sys_user_import_batch_preview_token"),
        "sys_user_import_batch",
        ["preview_token"],
        unique=True,
    )
    op.create_index(
        op.f("ix_sys_user_import_batch_status"),
        "sys_user_import_batch",
        ["status"],
        unique=False,
    )

    # 3. sys_user_import_batch_log（spec §2.28 v2.2 P1，FK CASCADE）
    op.create_table(
        "sys_user_import_batch_log",
        sa.Column(
            "log_id",
            sa.String(length=64),
            nullable=False,
            comment="Snowflake ID",
        ),
        sa.Column("batch_id", sa.String(length=64), nullable=False),
        sa.Column("operator_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "event",
            sa.String(length=32),
            nullable=False,
            comment="事件：CREATED/PREVIEW_DONE/EXECUTE_START/CHUNK_PROGRESS/EXECUTE_FINISH/EXECUTE_FAILED/EXPIRED/CANCELLED",
        ),
        sa.Column("from_status", import_batch_status_type, nullable=True),
        sa.Column("to_status", import_batch_status_type, nullable=True),
        sa.Column(
            "detail",
            sa.JSON(),
            nullable=False,
            comment="事件详情：chunk_index / chunk_size / failed_in_chunk / error_message / reason 等",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["sys_user_import_batch.batch_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("log_id"),
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

    # 4. sys_user_export_task（spec §2.31 v2.2 P1-5）
    op.create_table(
        "sys_user_export_task",
        sa.Column(
            "export_id",
            sa.String(length=64),
            nullable=False,
            comment="Snowflake ID",
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
            comment="操作理由（spec §2.30）：与 import batch.reason 对称",
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
            "status", export_task_status_type, nullable=False, server_default="CREATED"
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("export_id"),
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

    # 5. sys_user.employee_no（spec §2.24，UNIQUE 索引，允许多个 NULL）
    op.add_column(
        "sys_user",
        sa.Column(
            "employee_no",
            sa.String(length=64),
            nullable=True,
            comment="员工工号（spec §2.24）：企业同步 / LDAP / ERP 对接，UNIQUE 但允许多个 NULL",
        ),
    )
    op.create_unique_constraint("uq_sys_user_employee_no", "sys_user", ["employee_no"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_sys_user_employee_no", "sys_user", type_="unique")
    op.drop_column("sys_user", "employee_no")

    op.drop_index(
        op.f("ix_sys_user_export_task_status"), table_name="sys_user_export_task"
    )
    op.drop_index(
        op.f("ix_sys_user_export_task_operator_id"), table_name="sys_user_export_task"
    )
    op.drop_index(
        op.f("ix_sys_user_export_task_created_at"), table_name="sys_user_export_task"
    )
    op.drop_table("sys_user_export_task")

    op.drop_index(
        op.f("ix_sys_user_import_batch_log_operator_id"),
        table_name="sys_user_import_batch_log",
    )
    op.drop_index(
        op.f("ix_sys_user_import_batch_log_created_at"),
        table_name="sys_user_import_batch_log",
    )
    op.drop_index(
        op.f("ix_sys_user_import_batch_log_batch_id"),
        table_name="sys_user_import_batch_log",
    )
    op.drop_table("sys_user_import_batch_log")

    op.drop_index(
        op.f("ix_sys_user_import_batch_status"), table_name="sys_user_import_batch"
    )
    op.drop_index(
        op.f("ix_sys_user_import_batch_preview_token"),
        table_name="sys_user_import_batch",
    )
    op.drop_index(
        op.f("ix_sys_user_import_batch_operator_id"),
        table_name="sys_user_import_batch",
    )
    op.drop_index(
        op.f("ix_sys_user_import_batch_created_at"), table_name="sys_user_import_batch"
    )
    op.drop_table("sys_user_import_batch")

    op.execute("DROP TYPE export_task_status")
    op.execute("DROP TYPE import_batch_status")
