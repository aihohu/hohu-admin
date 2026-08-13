"""add ai_agent.risk_appetite for per-agent risk classification

为 Agent 增加独立风险偏好配置，
ai_agent 表加 risk_appetite 字段（默认 'balanced'，向后兼容 MVP 矩阵）。

仅影响 high risk 的 dry_run_count 阈值：
  - conservative: high 永远 HITL
  - balanced（默认）: high + dry_run_count≤1 autonomous（MVP 行为）
  - aggressive: high 永远 autonomous
destructive / hitl_always / injection_hit 不受影响（安全底线）。

Revision ID: 51e74fc18eb0
Revises: 50db0bf83047
Create Date: 2026-07-20 18:56:26.062914

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "51e74fc18eb0"
down_revision: str | Sequence[str] | None = "50db0bf83047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 默认 'balanced'，老 agent 行迁移后自动 'balanced'（与 MVP 行为等价）
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
    # DB 层 CHECK 约束防任意字符串（与 Python Literal 类型双保险）
    op.create_check_constraint(
        "ck_ai_agent_risk_appetite",
        "ai_agent",
        "risk_appetite IN ('conservative', 'balanced', 'aggressive')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ai_agent_risk_appetite", "ai_agent", type_="check")
    op.drop_column("ai_agent", "risk_appetite")
