"""Add user transfer and file ownership security schema.

Revision ID: 0b2165376771
Revises: fba0cf4a5e82
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0b2165376771"
down_revision: str | Sequence[str] | None = "fba0cf4a5e82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade user transfer and file ownership security schema."""
    _upgrade_user_import_export()
    _upgrade_import_records_hash()
    _upgrade_sys_file_owner_tenant()


def downgrade() -> None:
    """Downgrade user transfer and file ownership security schema."""
    _downgrade_sys_file_owner_tenant()
    _downgrade_import_records_hash()
    _downgrade_user_import_export()


def _upgrade_user_import_export() -> None:
    """Upgrade schema."""
    # Create native PostgreSQL enums shared with the application state machines.
    # Raw DDL avoids asyncpg checkfirst issues with sa.Enum().create().
    op.execute(
        "CREATE TYPE import_batch_status AS ENUM "
        "('CREATED', 'PREVIEW_DONE', 'RUNNING', 'SUCCESS', "
        "'PARTIAL_SUCCESS', 'FAILED', 'EXPIRED', 'CANCELLED')"
    )
    op.execute(
        "CREATE TYPE export_task_status AS ENUM "
        "('CREATED', 'RUNNING', 'SUCCESS', 'FAILED', 'EXPIRED')"
    )

    # These column types reference enums already created by the raw DDL above.
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

    # Create the sys_user_import_batch aggregate root.
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
            comment="execute 时反查 batch 行，用于幂等控制",
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
            comment="原始上传文件的 storage_key",
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
            import_batch_status_type,
            nullable=False,
            server_default="CREATED",
            comment="导入批次状态机",
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

    # Create audit logs that are deleted with their import batch.
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

    # Create the user export task aggregate.
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
            comment="导出操作理由，与 import batch.reason 对称",
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

    # Add a nullable employee number with uniqueness for non-null values.
    op.add_column(
        "sys_user",
        sa.Column(
            "employee_no",
            sa.String(length=64),
            nullable=True,
            comment="员工工号，用于企业同步、LDAP 或 ERP 对接；UNIQUE 但允许多个 NULL",
        ),
    )
    op.create_unique_constraint("uq_sys_user_employee_no", "sys_user", ["employee_no"])


def _upgrade_import_records_hash() -> None:
    """Add the records hash used to verify preview consistency.

    Dry-run inserts the batch with its real hash, so existing rows only need the
    temporary default while the column is introduced.
    """
    op.add_column(
        "sys_user_import_batch",
        sa.Column(
            "records_hash",
            sa.String(length=64),
            nullable=False,
            server_default="",
            comment="records 序列化后的 sha256，用于执行前一致性校验",
        ),
    )
    # Future inserts always provide the real hash, so remove the temporary default.
    op.alter_column(
        "sys_user_import_batch",
        "records_hash",
        server_default=None,
    )


def _upgrade_sys_file_owner_tenant() -> None:
    """Add immutable owner and trusted tenant anchors to uploaded files."""
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
            nullable=False,
            server_default="0",
            comment="租户ID（当前单租户固定为0）",
        ),
    )

    # Do not backfill from create_by/user_name.  user_name is mutable and users
    # are hard-deleted, so a later account may reuse the same name.  Assigning
    # those legacy files would create an IDOR.  NULL is intentionally fail-closed;
    # only uploads made after this migration receive an authenticated owner ID.


def _downgrade_sys_file_owner_tenant() -> None:
    """Remove uploaded-file security anchors."""
    op.drop_column("sys_file", "tenant_id")
    op.drop_column("sys_file", "owner_user_id")


def _downgrade_import_records_hash() -> None:
    """Downgrade schema."""
    op.drop_column("sys_user_import_batch", "records_hash")


def _downgrade_user_import_export() -> None:
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
