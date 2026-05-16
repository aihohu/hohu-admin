from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class Config(Base):
    """系统配置模型"""

    __tablename__ = "sys_config"

    config_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="配置ID"
    )
    config_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="配置名称"
    )
    config_key: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, comment="配置键"
    )
    config_value: Mapped[str] = mapped_column(Text, nullable=False, comment="配置值")
    config_type: Mapped[str] = mapped_column(
        String(20), default="text", comment="配置类型：text/richtext/file"
    )
    config_group: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="配置分组"
    )
    status: Mapped[str] = mapped_column(
        String(2), default="1", comment="状态：1-启用，2-禁用"
    )
    is_public: Mapped[bool] = mapped_column(
        default=False, server_default="false", comment="是否公开访问"
    )
    remark: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="备注"
    )
    # 审计字段
    create_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="创建者"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    update_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="更新者"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )
