"""用户对路由决策的追加式反馈历史表。

与 ai_message.routing_feedback 配合：后者是当前态（覆盖更新），本表是历史轨迹.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class AiRoutingFeedback(Base):
    __tablename__ = "ai_routing_feedback"
    __table_args__ = (
        CheckConstraint(
            "feedback IN ('correct', 'wrong')",
            name="ck_ai_routing_feedback_type",
        ),
        Index(
            "ix_ai_routing_feedback_tenant_message_created",
            "tenant_id",
            "message_id",
            "create_time",
        ),
        Index(
            "ix_ai_routing_feedback_tenant_trace",
            "tenant_id",
            "trace_id",
        ),
        CheckConstraint(
            "(feedback = 'wrong' AND corrected_agent IS NOT NULL) "
            "OR (feedback = 'correct' AND corrected_agent IS NULL)",
            name="ck_ai_routing_feedback_correction_match",
        ),
        {"comment": "用户对路由决策的反馈历史轨迹（append-only）"},
    )

    feedback_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="租户ID；必须与反馈消息一致",
    )
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_agent: Mapped[str] = mapped_column(String(64), nullable=False)
    feedback: Mapped[str] = mapped_column(String(16), nullable=False)
    corrected_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
