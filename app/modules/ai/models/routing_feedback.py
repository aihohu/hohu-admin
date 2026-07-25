"""spec §7.1c: ai_routing_feedback 表 — 用户对路由决策的反馈历史轨迹（append-only）.

与 ai_message.routing_feedback 配合：后者是当前态（覆盖更新），本表是历史轨迹.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
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
        CheckConstraint(
            "(feedback = 'wrong' AND corrected_agent IS NOT NULL) "
            "OR (feedback = 'correct' AND corrected_agent IS NULL)",
            name="ck_ai_routing_feedback_correction_match",
        ),
    )

    feedback_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id
    )
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    original_agent: Mapped[str] = mapped_column(String(64), nullable=False)
    feedback: Mapped[str] = mapped_column(String(16), nullable=False)
    corrected_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
