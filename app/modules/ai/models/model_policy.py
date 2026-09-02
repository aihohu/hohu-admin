"""Tenant eligibility policy for platform-global AI models."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TenantAiModelPolicy(Base):
    """Explicitly grants one tenant access to one platform model."""

    __tablename__ = "tenant_ai_model_policy"
    __table_args__ = (
        CheckConstraint(
            "daily_quota_per_user IS NULL OR daily_quota_per_user > 0",
            name="ck_tenant_ai_model_policy_positive_quota",
        ),
        Index(
            "uq_tenant_ai_model_policy_enabled_default",
            "tenant_id",
            unique=True,
            postgresql_where=text("enabled = true AND is_default = true"),
        ),
        Index(
            "ix_tenant_ai_model_policy_tenant_enabled_model",
            "tenant_id",
            "enabled",
            "model_id",
        ),
    )

    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_tenant.tenant_id", ondelete="CASCADE"),
        primary_key=True,
        comment="获授权租户ID",
    )
    model_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ai_model.model_id", ondelete="CASCADE"),
        primary_key=True,
        comment="平台全局模型ID",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="租户是否可使用该模型",
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="租户默认聊天模型",
    )
    daily_quota_per_user: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="该租户内单用户日配额；NULL 表示使用上层配额",
    )
