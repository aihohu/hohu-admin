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
            "tenant_id",
            "confirmation_id",
            name="uq_ai_prepared_action_tenant_confirmation_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "execute_tool_call_id",
            name="uq_ai_prepared_action_tenant_execute_tool_call_id",
        ),
        Index(
            "ix_ai_prepared_action_tenant_conversation_status_expires",
            "tenant_id",
            "conversation_id",
            "status",
            "expires_at",
        ),
        Index(
            "ix_ai_prepared_action_tenant_source_status",
            "tenant_id",
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
    tool_codes: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    subject_refs: Mapped[list[dict[str, str]] | None] = mapped_column(
        JSON, nullable=True
    )
    subject_refs_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_scope_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolver_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    projection_dependency_message_ids: Mapped[list[str] | None] = mapped_column(
        JSON,
        nullable=True,
        default=list,
        comment="Immutable prior assistant message IDs used as model context",
    )
    presentation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_user_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_code: Mapped[str] = mapped_column(String(64), nullable=False)
    resolved_model_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resolved_provider_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    guard_owner_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    command_action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="send", server_default=text("'send'")
    )
    risk_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default="high", server_default=text("'high'")
    )
    chip_target: Mapped[str | None] = mapped_column(String(255), nullable=True)

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
    result_data: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    result_ui: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
