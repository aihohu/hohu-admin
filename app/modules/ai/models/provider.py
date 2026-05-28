from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class AiProvider(Base):
    __tablename__ = "ai_provider"

    provider_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="提供商ID"
    )
    provider_code: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
        comment="提供商标识：openai / anthropic / deepseek",
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="显示名称")
    api_key: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="API Key（加密存储）"
    )
    base_url: Mapped[str] = mapped_column(
        String(500), nullable=True, comment="默认 API 地址"
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, comment="是否启用"
    )
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="扩展配置")
    create_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="创建者"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )
