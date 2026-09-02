from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class AiModel(Base):
    __tablename__ = "ai_model"
    __table_args__ = (
        UniqueConstraint("provider_id", "name", name="uq_ai_model_provider_name"),
        Index(
            "ix_ai_model_capabilities",
            "capabilities",
            postgresql_using="gin",
            postgresql_ops={"capabilities": "jsonb_path_ops"},
        ),
    )

    model_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="模型ID"
    )
    provider_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ai_provider.provider_id", ondelete="CASCADE"),
        nullable=False,
        comment="所属提供商ID",
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="模型名称")
    capabilities: Mapped[dict | None] = mapped_column(
        postgresql.JSONB,
        nullable=False,
        comment='能力标签，如 ["text","vision","image-gen"]',
    )
    base_url: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="模型级 API 地址（覆盖提供商默认）"
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否启用"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="排序（越小越靠前）"
    )
    config: Mapped[dict | None] = mapped_column(
        postgresql.JSONB, nullable=True, comment="扩展配置"
    )
    create_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="创建者"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )
