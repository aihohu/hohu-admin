"""add sys_data_scope_demo table

Revision ID: bf244f9a8b76
Revises: fbb2836b2e4b
Create Date: 2026-07-01 15:42:53.022715

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bf244f9a8b76"
down_revision: Union[str, Sequence[str], None] = "fbb2836b2e4b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "sys_data_scope_demo",
        sa.Column("demo_id", sa.BigInteger(), nullable=False, comment="演示数据ID"),
        sa.Column("title", sa.String(length=100), nullable=False, comment="标题"),
        sa.Column("content", sa.Text(), nullable=True, comment="内容"),
        sa.Column(
            "dept_id",
            sa.BigInteger(),
            nullable=False,
            comment="所属部门ID（数据权限锚点）",
        ),
        sa.Column(
            "create_by",
            sa.BigInteger(),
            nullable=False,
            comment="创建人 user_id（SELF scope 锚点，存 ID 而非 user_name）",
        ),
        sa.Column(
            "status",
            sa.String(length=2),
            nullable=False,
            comment="状态：1-启用，2-禁用",
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
        sa.PrimaryKeyConstraint("demo_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("sys_data_scope_demo")
