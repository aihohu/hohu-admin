"""add runner_id to sys_job_log

Revision ID: fba0cf4a5e82
Revises: 654cab643a43
Create Date: 2026-07-26 15:06:15.348679

为定时任务执行日志加 `runner_id`（写入此 log 的进程标识 uuid4）+ `(status, start_time)`
复合索引。孤儿任务日志守护协程用此字段区分本进程长任务 vs 上一进程遗留孤儿。

详见 docs/specs/2026-07-02-orphan-job-log-monitor.md。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'fba0cf4a5e82'
down_revision: Union[str, Sequence[str], None] = '654cab643a43'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'sys_job_log',
        sa.Column(
            'runner_id',
            sa.String(length=64),
            nullable=True,
            comment='写入此日志的执行进程标识（uuid4，孤儿守护用）',
        ),
    )
    op.create_index(
        'ix_sys_job_log_status_start_time',
        'sys_job_log',
        ['status', 'start_time'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_sys_job_log_status_start_time', table_name='sys_job_log')
    op.drop_column('sys_job_log', 'runner_id')
