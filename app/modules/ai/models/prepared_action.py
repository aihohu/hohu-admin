"""Durable authorization facts created by Gateway-owned prepared flows."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class AiPreparedAction(Base):
    """Frozen preview-to-execute authorization owned by the AI Gateway."""

    __tablename__ = "ai_prepared_action"
    __table_args__ = (
        CheckConstraint(
            "status IN ('prepared', 'pending_confirmation', 'approved', "
            "'running', 'succeeded', 'failed', 'rejected', 'expired')",
            name="ck_ai_prepared_action_status",
        ),
        UniqueConstraint(
            "confirmation_id", name="uq_ai_prepared_action_confirmation_id"
        ),
        UniqueConstraint(
            "execute_tool_call_id", name="uq_ai_prepared_action_execute_tool_call_id"
        ),
        Index(
            "ix_ai_prepared_action_conversation_status_expires",
            "conversation_id",
            "status",
            "expires_at",
        ),
        Index(
            "ix_ai_prepared_action_source_status",
            "source_user_message_id",
            "status",
        ),
    )

    action_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="Snowflake action ID"
    )
    confirmation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending_confirmation",
        server_default=text("'pending_confirmation'"),
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    interaction_flow: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    dispatch_mode: Mapped[str] = mapped_column(String(32), nullable=False)

    prepare_tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execute_tool_call_id: Mapped[str] = mapped_column(String(64), nullable=False)
    execute_tool_name: Mapped[str] = mapped_column(String(128), nullable=False)

    frozen_args: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    args_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    presentation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_user_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_code: Mapped[str] = mapped_column(String(64), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    approved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
