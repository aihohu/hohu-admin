from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.id_generator import next_id
from app.db.base import Base

if TYPE_CHECKING:
    from .conversation import AiConversation


class AiMessage(Base):
    __tablename__ = "ai_message"
    __table_args__ = (
        CheckConstraint(
            "routing_feedback IS NULL OR routing_feedback IN ('correct', 'wrong')",
            name="ck_ai_message_routing_feedback",
        ),
        Index(
            "ix_ai_message_active_history",
            "conversation_id",
            "create_time",
            "message_id",
            postgresql_where=text("is_active = true"),
        ),
        Index(
            "uq_ai_message_assistant_run",
            "conversation_id",
            "trace_id",
            unique=True,
            postgresql_where=text("role = 'assistant' AND trace_id IS NOT NULL"),
        ),
        Index("ix_ai_message_supersedes_message_id", "supersedes_message_id"),
    )

    message_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="消息ID"
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ai_conversation.conversation_id", ondelete="CASCADE"),
        nullable=False,
        comment="所属会话",
    )
    parent_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="父消息（工具调用关联链）"
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="角色：user / assistant / system / tool"
    )
    message_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="text",
        comment="类型：text / tool_call / tool_result",
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True, comment="消息内容")
    tokens_input: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="输入 token 数"
    )
    tokens_output: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="输出 token 数"
    )
    parts: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment="结构化消息内容（含图片、文件等）"
    )
    tool_calls: Mapped[list | None] = mapped_column(
        JSON, nullable=True, comment="工具调用记录列表（名称、参数、结果）"
    )
    trace_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="追踪ID，与 ai_operation_log 关联"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="当前 active projection；inactive 仅供审计",
    )
    supersedes_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="本消息替换的原 message_id；不复用 parent_message_id",
    )
    agent_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="本条消息实际处理的 Agent code，用于按消息粒度还原 Agent",
    )
    routing_feedback: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="用户路由反馈：correct、wrong 或 null",
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )

    conversation: Mapped["AiConversation"] = relationship(
        "AiConversation", back_populates="messages"
    )
