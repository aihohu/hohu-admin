"""add queued_at and hitl_wait_ms to ai_operation_log (spec S-3)

Revision ID: 3b03d2eccf39
Revises: c7d8e9f0a1b2
Create Date: 2026-07-11 11:21:12.689020

重整操作日志时间字段语义：
  - queued_at（新增）: 行级创建时间，含 HITL 等待之前
  - started_at（改为 nullable + 去 server_default）: 业务执行起点
  - hitl_wait_ms（新增）: HITL 等待耗时，autonomous 流为 None
  - duration_ms: 语义不变（业务耗时），comment 更新

新索引：
  - idx_ai_op_log_user_queued (user_id, queued_at) 替代 idx_ai_op_log_user_started
  - idx_ai_op_log_trace 改用 queued_at（trace 时间排序）
  - idx_ai_op_log_security 改用 queued_at
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b03d2eccf39"
down_revision: str | Sequence[str] | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. 新增 queued_at（NOT NULL，server_default=now()，回填旧行用 started_at 旧值）
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
    # 2. 新增 hitl_wait_ms（nullable，autonomous 流为 NULL）
    op.add_column(
        "ai_operation_log",
        sa.Column(
            "hitl_wait_ms",
            sa.Integer(),
            nullable=True,
            comment="HITL 等待耗时（autonomous 流为 None）",
        ),
    )
    # 3. started_at 改 nullable + 去 server_default（业务起点，由代码显式写入）
    op.alter_column(
        "ai_operation_log",
        "started_at",
        existing_type=sa.DateTime(),
        nullable=True,
        comment="业务执行起点（HITL approve 后 / autonomous 入库后）",
        existing_comment="单次 tool 调用开始时间",
        server_default=None,
    )
    # 4. duration_ms comment 更新（语义不变，仅说明不含 HITL 等待）
    op.alter_column(
        "ai_operation_log",
        "duration_ms",
        existing_type=sa.Integer(),
        comment="业务执行耗时，不含 HITL 等待",
        existing_nullable=True,
    )

    # 5. 索引重整：旧索引用 started_at，新索引用 queued_at（行级时间，更稳定）
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


def downgrade() -> None:
    """Downgrade schema."""
    # 还原索引
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

    # 还原 duration_ms comment（虽然语义没变，恢复原 comment）
    op.alter_column(
        "ai_operation_log",
        "duration_ms",
        existing_type=sa.Integer(),
        comment=None,
        existing_nullable=True,
    )
    # 还原 started_at（NOT NULL + server_default=now()）
    op.alter_column(
        "ai_operation_log",
        "started_at",
        existing_type=sa.DateTime(),
        nullable=False,
        comment="单次 tool 调用开始时间",
        existing_comment="业务执行起点（HITL approve 后 / autonomous 入库后）",
        server_default=sa.text("now()"),
    )
    # 删除新增列
    op.drop_column("ai_operation_log", "hitl_wait_ms")
    op.drop_column("ai_operation_log", "queued_at")
