"""create ai_model table

Revision ID: a1b2c3d4e5f6
Revises: bef2b72de182
Create Date: 2026-05-28 12:00:00.000000

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "bef2b72de182"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ai_model table and migrate data from config.models."""
    op.create_table(
        "ai_model",
        sa.Column("model_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "provider_id",
            sa.BigInteger(),
            sa.ForeignKey("ai_provider.provider_id", ondelete="CASCADE"),
            nullable=False,
            comment="所属提供商ID",
        ),
        sa.Column("name", sa.String(100), nullable=False, comment="模型名称"),
        sa.Column(
            "capabilities",
            postgresql.JSONB(),
            nullable=False,
            comment='能力标签，如 ["text","vision","image-gen"]',
        ),
        sa.Column(
            "base_url",
            sa.String(500),
            nullable=True,
            comment="模型级 API 地址（覆盖提供商默认）",
        ),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("config", postgresql.JSONB(), nullable=True, comment="扩展配置"),
        sa.Column("create_by", sa.String(64), nullable=True, comment="创建者"),
        sa.Column(
            "create_time",
            sa.DateTime(),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "update_time",
            sa.DateTime(),
            server_default=sa.func.now(),
        ),
    )

    # 唯一约束：同一提供商下模型名称不重复
    op.create_unique_constraint(
        "uq_ai_model_provider_name", "ai_model", ["provider_id", "name"]
    )

    # GIN 索引用于 capabilities JSONB 查询
    op.execute(
        "CREATE INDEX ix_ai_model_capabilities ON ai_model USING gin (capabilities jsonb_path_ops)"
    )

    # 更新 provider 列注释
    op.alter_column(
        "ai_provider",
        "base_url",
        existing_type=sa.VARCHAR(length=500),
        comment="默认 API 地址",
        existing_comment="API 地址（OpenAI 兼容协议留空用默认）",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_provider",
        "config",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        comment="扩展配置",
        existing_comment="扩展配置（温度、token 限额等）",
        existing_nullable=True,
    )

    # 数据迁移：从 config.models 迁移到 ai_model 表
    _migrate_models_data()


def _migrate_models_data() -> None:
    """将 ai_provider.config.models 中的模型数据迁移到 ai_model 表"""
    from snowflake import SnowflakeGenerator  # noqa: PLC0415

    gen = SnowflakeGenerator(instance=1)
    conn = op.get_bind()

    providers = conn.execute(
        sa.text("SELECT provider_id, config FROM ai_provider")
    ).fetchall()

    for provider_id, config in providers:
        if not config or "models" not in config:
            continue

        models_list = config["models"]
        if not isinstance(models_list, list):
            continue

        for idx, m in enumerate(models_list):
            if isinstance(m, str):
                model_name = m
            elif isinstance(m, dict):
                model_name = m.get("model") or m.get("name", "")
            else:
                continue

            if not model_name:
                continue

            # 幂等检查
            exists = conn.execute(
                sa.text(
                    "SELECT 1 FROM ai_model WHERE provider_id = :pid AND name = :name"
                ),
                {"pid": provider_id, "name": model_name},
            ).fetchone()

            if exists:
                continue

            model_id = next(gen)
            conn.execute(
                sa.text(
                    """INSERT INTO ai_model (model_id, provider_id, name, capabilities, is_enabled, sort_order)
                   VALUES (:mid, :pid, :name, :caps, true, :sort)"""
                ),
                {
                    "mid": model_id,
                    "pid": provider_id,
                    "name": model_name,
                    "caps": json.dumps(["text"]),
                    "sort": idx,
                },
            )

    # 迁移完成后，从 config 中删除 models 字段
    for provider_id, config in providers:
        if not config or "models" not in config:
            continue
        new_config = {k: v for k, v in config.items() if k != "models"}
        conn.execute(
            sa.text("UPDATE ai_provider SET config = :cfg WHERE provider_id = :pid"),
            {
                "cfg": json.dumps(new_config) if new_config else None,
                "pid": provider_id,
            },
        )


def downgrade() -> None:
    """Drop ai_model table."""
    op.execute("DROP INDEX IF EXISTS ix_ai_model_capabilities")
    op.drop_constraint("uq_ai_model_provider_name", "ai_model", type_="unique")
    op.drop_table("ai_model")

    op.alter_column(
        "ai_provider",
        "config",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        comment="扩展配置（温度、token 限额等）",
        existing_comment="扩展配置",
        existing_nullable=True,
    )
    op.alter_column(
        "ai_provider",
        "base_url",
        existing_type=sa.VARCHAR(length=500),
        comment="API 地址（OpenAI 兼容协议留空用默认）",
        existing_comment="默认 API 地址",
        existing_nullable=True,
    )
