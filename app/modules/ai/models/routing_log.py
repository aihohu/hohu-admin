"""Supervisor 路由决策审计表。"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class AiRoutingLog(Base):
    """覆盖所有 ``/ai/chat`` 请求类型的路由决策日志。"""

    __tablename__ = "ai_routing_log"

    log_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=next_id)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    input_message_hash: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment=(
            "HMAC-SHA256(server_secret + user_id + message)；运维调试用，非法证取证"
        ),
    )
    candidates: Mapped[list] = mapped_column(JSONB, nullable=False)
    llm_choice: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_agent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment=(
            "llm_resolved / clarification / session_sticky / manual_override / "
            "supervisor_disabled / safety_blocked / quota_exceeded / no_provider / "
            "no_candidates / legacy_null_mode"
        ),
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_log_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment=("为多 Agent 协作预留；当前始终为 NULL"),
    )
    plan_step_index: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="为多 Agent 协作预留；当前始终为 NULL",
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
