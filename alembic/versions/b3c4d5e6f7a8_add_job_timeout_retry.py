"""add job timeout/retry/run_on_enable fields

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-06-16 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'sys_job',
        sa.Column(
            'timeout_seconds',
            sa.Integer(),
            nullable=True,
            comment='单次执行超时秒数（空表示不限）',
        ),
    )
    op.add_column(
        'sys_job',
        sa.Column(
            'max_retries',
            sa.Integer(),
            nullable=False,
            server_default='0',
            comment='失败重试次数（0 表示不重试）',
        ),
    )
    op.add_column(
        'sys_job',
        sa.Column(
            'run_on_enable',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
            comment='启用时是否立即执行一次',
        ),
    )
    op.add_column(
        'sys_job_log',
        sa.Column(
            'attempt_count',
            sa.Integer(),
            nullable=False,
            server_default='1',
            comment='本次触发实际执行次数（含重试）',
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('sys_job_log', 'attempt_count')
    op.drop_column('sys_job', 'run_on_enable', if_exists=True)
    op.drop_column('sys_job', 'max_retries')
    op.drop_column('sys_job', 'timeout_seconds')
