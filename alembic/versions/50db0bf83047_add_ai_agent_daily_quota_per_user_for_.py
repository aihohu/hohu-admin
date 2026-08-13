"""add ai_agent.daily_quota_per_user for per-agent L2 quota

为 Agent 增加每用户日配额上限，
ai_agent 表加 daily_quota_per_user 字段（nullable，None=仅走全局 L2）。

叠加不替代全局 L2：executor 先 check_l2_daily_quota（全局），再 check_l2_agent_quota
（per-agent，仅当本字段非 None）。任一层超限抛 AI_DAILY_QUOTA_EXHAUSTED。
decr_quota 扩展为同步回滚两层 key（修订 S-11 对称原则）。

Revision ID: 50db0bf83047
Revises: 3b03d2eccf39
Create Date: 2026-07-20 11:24:11.126204

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "50db0bf83047"
down_revision: str | Sequence[str] | None = "3b03d2eccf39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_agent",
        sa.Column(
            "daily_quota_per_user",
            sa.Integer(),
            nullable=True,
            comment="Agent 日配额上限，None 表示仅使用全局 L2 配额",
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_agent", "daily_quota_per_user")
