from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.id_generator import next_id
from app.db.base import Base

if TYPE_CHECKING:
    from .message import AiMessage


class AiConversation(Base):
    __tablename__ = "ai_conversation"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "conversation_id",
            name="uq_ai_conversation_tenant_conversation_id",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "user_id"),
            ("sys_user.tenant_id", "sys_user.user_id"),
            name="fk_ai_conversation_tenant_user",
            ondelete="CASCADE",
        ),
        Index(
            "ix_ai_conversation_tenant_user_updated",
            "tenant_id",
            "user_id",
            "update_time",
            "conversation_id",
        ),
    )

    conversation_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="会话ID"
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="租户ID；必须由可信 TenantContext 显式写入",
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="所属用户",
    )
    title: Mapped[str] = mapped_column(
        String(200), nullable=False, default="新对话", comment="会话标题"
    )
    model_name: Mapped[str] = mapped_column(
        String(100), nullable=False, default="openai:gpt-4o", comment="使用的模型标识"
    )
    system_prompt: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="系统提示词（Agent instructions）"
    )
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, comment="状态：0=活跃, 1=归档"
    )
    agent_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="绑定的 Agent code"
    )
    trace_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="会话级追踪ID，串联 ai_operation_log"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Soft deletion timestamp; NULL means active",
    )

    messages: Mapped[list["AiMessage"]] = relationship(
        "AiMessage", back_populates="conversation", cascade="all, delete-orphan"
    )
