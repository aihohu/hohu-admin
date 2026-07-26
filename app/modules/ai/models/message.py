from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
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
    agent_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="spec §7.1b: 本条消息实际处理的 Agent code（按消息粒度还原 Agent）",
    )
    routing_feedback: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="spec §7.1b: 用户路由反馈 'correct' / 'wrong' / null",
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )

    conversation: Mapped["AiConversation"] = relationship(
        "AiConversation", back_populates="messages"
    )
