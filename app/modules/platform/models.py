from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class PlatformPrincipal(Base):
    """Platform-global principal, deliberately unrelated to tenant users and roles."""

    __tablename__ = "sys_platform_principal"
    __table_args__ = (
        UniqueConstraint("principal_name", name="uq_platform_principal_principal_name"),
        CheckConstraint("status IN ('1', '2')", name="ck_platform_principal_status"),
        CheckConstraint("row_version >= 1", name="ck_platform_principal_row_version"),
        CheckConstraint(
            "principal_name = lower(btrim(principal_name))",
            name="ck_platform_principal_normalized_name",
        ),
        CheckConstraint(
            "principal_name ~ '^[a-z][a-z0-9_-]{2,63}$'",
            name="ck_platform_principal_name_format",
        ),
        CheckConstraint(
            "jsonb_typeof(permissions) = 'array'",
            name="ck_platform_principal_permissions_array",
        ),
        Index("ix_platform_principal_status", "status"),
    )

    principal_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, autoincrement=False
    )
    principal_name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(2), nullable=False, default="1", server_default="1"
    )
    permissions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    row_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        server_onupdate=FetchedValue(),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PlatformAuditLog(Base):
    """Append-only global audit event for platform control-plane authorization."""

    __tablename__ = "sys_platform_audit_log"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('authorized', 'completed', 'denied')",
            name="ck_platform_audit_event_type",
        ),
        CheckConstraint(
            "(event_type = 'completed' AND authorization_audit_id IS NOT NULL) "
            "OR (event_type IN ('authorized', 'denied') "
            "AND authorization_audit_id IS NULL)",
            name="ck_platform_audit_authorization_lineage",
        ),
        CheckConstraint(
            "event_type = 'denied' OR "
            "(reason IS NOT NULL AND btrim(reason) <> '' "
            "AND ticket_id IS NOT NULL AND btrim(ticket_id) <> '' "
            "AND correlation_id IS NOT NULL AND btrim(correlation_id) <> '')",
            name="ck_platform_audit_required_context",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_platform_audit_duration",
        ),
        CheckConstraint(
            "target_tenant_id IS NULL OR target_tenant_id >= 0",
            name="ck_platform_audit_target_tenant_id",
        ),
        CheckConstraint(
            "(event_type = 'authorized' AND status_code IS NULL "
            "AND duration_ms IS NULL AND denial_code IS NULL) OR "
            "(event_type = 'completed' AND status_code BETWEEN 100 AND 599 "
            "AND duration_ms IS NOT NULL AND denial_code IS NULL) OR "
            "(event_type = 'denied' AND status_code BETWEEN 400 AND 599 "
            "AND duration_ms IS NULL AND denial_code IS NOT NULL)",
            name="ck_platform_audit_event_fields",
        ),
        CheckConstraint(
            "(request_summary IS NULL OR "
            "jsonb_typeof(request_summary) = 'object') AND "
            "(result_summary IS NULL OR "
            "jsonb_typeof(result_summary) = 'object')",
            name="ck_platform_audit_summary_objects",
        ),
        Index("ix_platform_audit_correlation", "correlation_id"),
        Index("ix_platform_audit_actor_time", "actor_principal_id", "created_at"),
        Index("ix_platform_audit_target_time", "target_tenant_id", "created_at"),
        Index(
            "uq_platform_audit_one_completion",
            "authorization_audit_id",
            unique=True,
            postgresql_where=text("authorization_audit_id IS NOT NULL"),
        ),
    )

    audit_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, autoincrement=False
    )
    authorization_audit_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("sys_platform_audit_log.audit_id", ondelete="RESTRICT"),
        nullable=True,
    )
    actor_principal_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_platform_principal.principal_id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_name: Mapped[str] = mapped_column(String(64), nullable=False)
    permission: Mapped[str] = mapped_column(String(96), nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(256), nullable=False)
    target_tenant_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ticket_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(50), nullable=True)
    denial_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
