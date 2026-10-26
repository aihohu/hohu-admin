"""Unactivated user field-policy candidates and durable authorization state."""

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class UserFieldPolicyGlobalGuard(Base):
    __tablename__ = "sys_user_field_policy_global_guard"
    __table_args__ = (
        CheckConstraint("guard_id = 1", name="ck_ufp_guard_singleton"),
        CheckConstraint("epoch >= 1", name="ck_ufp_guard_epoch"),
    )

    guard_id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, autoincrement=False
    )
    epoch: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserFieldPolicyState(Base):
    __tablename__ = "sys_user_field_policy_state"
    __table_args__ = (
        CheckConstraint("object_type = 'user'", name="ck_ufp_state_object"),
        CheckConstraint("mode IN ('legacy', 'ready')", name="ck_ufp_state_mode"),
        CheckConstraint("ever_enforced = false", name="ck_ufp_state_not_enforced"),
        CheckConstraint("epoch >= 1", name="ck_ufp_state_epoch"),
        CheckConstraint(
            "btrim(catalog_revision) <> ''", name="ck_ufp_state_catalog_revision"
        ),
        CheckConstraint(
            "catalog_digest ~ '^[0-9a-f]{64}$'", name="ck_ufp_state_catalog_digest"
        ),
        CheckConstraint(
            "candidate_manifest_digest IS NULL OR "
            "candidate_manifest_digest ~ '^[0-9a-f]{64}$'",
            name="ck_ufp_state_candidate_digest",
        ),
        CheckConstraint(
            "active_manifest_digest IS NULL", name="ck_ufp_state_no_active_manifest"
        ),
        CheckConstraint(
            "mode <> 'ready' OR candidate_manifest_digest IS NOT NULL",
            name="ck_ufp_state_ready_manifest",
        ),
        Index("ix_ufp_state_mode_tenant", "mode", "tenant_id"),
    )

    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_tenant.tenant_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    object_type: Mapped[str] = mapped_column(
        String(16), default="user", server_default="user"
    )
    mode: Mapped[str] = mapped_column(
        String(16), default="legacy", server_default="legacy"
    )
    ever_enforced: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    epoch: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    catalog_revision: Mapped[str] = mapped_column(String(64))
    catalog_digest: Mapped[str] = mapped_column(CHAR(64))
    candidate_manifest_digest: Mapped[str | None] = mapped_column(CHAR(64))
    active_manifest_digest: Mapped[str | None] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserFieldPolicyRevision(Base):
    __tablename__ = "sys_user_field_policy_revision"
    __table_args__ = (
        ForeignKeyConstraint(
            ("tenant_id", "role_id"),
            ("sys_role.tenant_id", "sys_role.role_id"),
            name="fk_ufp_revision_role",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "role_id", "revision_no", name="uq_ufp_revision_number"
        ),
        UniqueConstraint(
            "tenant_id", "role_id", "revision_id", name="uq_ufp_revision_identity"
        ),
        CheckConstraint("revision_no >= 1", name="ck_ufp_revision_number"),
        CheckConstraint(
            "btrim(catalog_revision) <> ''", name="ck_ufp_revision_catalog_revision"
        ),
        CheckConstraint(
            "catalog_digest ~ '^[0-9a-f]{64}$'", name="ck_ufp_revision_catalog_digest"
        ),
        CheckConstraint(
            "rules_digest ~ '^[0-9a-f]{64}$'", name="ck_ufp_revision_rules_digest"
        ),
        CheckConstraint(
            "CASE WHEN jsonb_typeof(rules) = 'array' "
            "THEN jsonb_array_length(rules) <= 256 ELSE false END "
            "AND octet_length(rules::text) <= 65536",
            name="ck_ufp_revision_rules_budget",
        ),
        CheckConstraint(
            "(source_digest IS NULL AND template_revision IS NULL) OR "
            "(source_digest IS NOT NULL AND template_revision IS NOT NULL "
            "AND source_digest ~ '^[0-9a-f]{64}$' "
            "AND btrim(template_revision) <> '')",
            name="ck_ufp_revision_source_pair",
        ),
        CheckConstraint("created_by_user_id > 0", name="ck_ufp_revision_actor"),
    )

    revision_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger)
    role_id: Mapped[int] = mapped_column(BigInteger)
    revision_no: Mapped[int] = mapped_column(BigInteger)
    catalog_revision: Mapped[str] = mapped_column(String(64))
    catalog_digest: Mapped[str] = mapped_column(CHAR(64))
    rules: Mapped[list[dict]] = mapped_column(JSONB)
    rules_digest: Mapped[str] = mapped_column(CHAR(64))
    source_digest: Mapped[str | None] = mapped_column(CHAR(64))
    template_revision: Mapped[str | None] = mapped_column(String(64))
    created_by_user_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserFieldPolicyBinding(Base):
    __tablename__ = "sys_user_field_policy_binding"
    __table_args__ = (
        ForeignKeyConstraint(
            ("tenant_id", "role_id"),
            ("sys_role.tenant_id", "sys_role.role_id"),
            name="fk_ufp_binding_role",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "role_id", "revision_id"),
            (
                "sys_user_field_policy_revision.tenant_id",
                "sys_user_field_policy_revision.role_id",
                "sys_user_field_policy_revision.revision_id",
            ),
            name="fk_ufp_binding_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version >= 1", name="ck_ufp_binding_version"),
    )

    tenant_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision_id: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(BigInteger)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserFieldPolicyAudit(Base):
    __tablename__ = "sys_user_field_policy_audit"
    __table_args__ = (
        ForeignKeyConstraint(
            ("tenant_id", "role_id", "revision_id"),
            (
                "sys_user_field_policy_revision.tenant_id",
                "sys_user_field_policy_revision.role_id",
                "sys_user_field_policy_revision.revision_id",
            ),
            name="fk_ufp_audit_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "action IN ('publish', 'archive', 'ready')", name="ck_ufp_audit_action"
        ),
        CheckConstraint("actor_user_id > 0", name="ck_ufp_audit_actor"),
        CheckConstraint("btrim(reason) <> ''", name="ck_ufp_audit_reason"),
        CheckConstraint("btrim(correlation_id) <> ''", name="ck_ufp_audit_correlation"),
        CheckConstraint(
            "before_digest IS NULL OR before_digest ~ '^[0-9a-f]{64}$'",
            name="ck_ufp_audit_before_digest",
        ),
        CheckConstraint(
            "after_digest ~ '^[0-9a-f]{64}$'", name="ck_ufp_audit_after_digest"
        ),
        CheckConstraint(
            "revision_id IS NULL OR role_id IS NOT NULL",
            name="ck_ufp_audit_revision_role",
        ),
        CheckConstraint(
            "(action = 'ready' AND role_id IS NULL AND revision_id IS NULL) OR "
            "(action IN ('publish', 'archive') AND role_id IS NOT NULL "
            "AND revision_id IS NOT NULL)",
            name="ck_ufp_audit_action_target",
        ),
        CheckConstraint(
            "before_version >= 0 AND after_version >= 1 "
            "AND after_version > before_version",
            name="ck_ufp_audit_versions",
        ),
        CheckConstraint("epoch >= 1", name="ck_ufp_audit_epoch"),
        Index("ix_ufp_audit_tenant_role", "tenant_id", "role_id", "audit_id"),
        Index("ix_ufp_audit_tenant_actor", "tenant_id", "actor_user_id", "audit_id"),
    )

    audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=next_id)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sys_tenant.tenant_id", ondelete="RESTRICT")
    )
    role_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_user_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(500))
    correlation_id: Mapped[str] = mapped_column(String(128))
    before_digest: Mapped[str | None] = mapped_column(CHAR(64))
    after_digest: Mapped[str] = mapped_column(CHAR(64))
    revision_id: Mapped[int | None] = mapped_column(BigInteger)
    before_version: Mapped[int] = mapped_column(BigInteger)
    after_version: Mapped[int] = mapped_column(BigInteger)
    epoch: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
