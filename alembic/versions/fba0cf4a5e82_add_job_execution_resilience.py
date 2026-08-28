"""Add job execution resilience schema after the v0.1.4 boundary.

Revision ID: fba0cf4a5e82
Revises: bf244f9a8b76
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fba0cf4a5e82"
down_revision: str | Sequence[str] | None = "bf244f9a8b76"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade the job execution resilience schema."""
    _upgrade_job_execution_resilience()


def downgrade() -> None:
    """Downgrade the job execution resilience schema."""
    _downgrade_job_execution_resilience()


def _upgrade_job_execution_resilience() -> None:
    """Upgrade schema."""
    op.add_column(
        "sys_job_log",
        sa.Column(
            "runner_id",
            sa.String(length=64),
            nullable=True,
            comment="写入此日志的执行进程标识（uuid4，孤儿守护用）",
        ),
    )
    op.create_index(
        "ix_sys_job_log_status_start_time",
        "sys_job_log",
        ["status", "start_time"],
        unique=False,
    )


def _downgrade_job_execution_resilience() -> None:
    """Downgrade schema."""
    op.drop_index("ix_sys_job_log_status_start_time", table_name="sys_job_log")
    op.drop_column("sys_job_log", "runner_id")
