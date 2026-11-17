"""Generic policy facts; the existing user-policy rows remain the only G/B."""

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


def _digest(name, *, nullable=False):
    expression = f"{name} ~ '^[0-9a-f]{{64}}$'"
    if nullable:
        expression = f"{name} IS NULL OR ({expression})"
    return CheckConstraint(expression, name="ck_gfp_" + name)


def _fk(local, target, remote=None, *, name=None, alter=False):
    return ForeignKeyConstraint(
        local,
        [target + "." + c for c in (remote or local)],
        ondelete="RESTRICT",
        name=name,
        use_alter=alter,
    )


class _Created:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FieldCatalogSnapshot(_Created, Base):
    __tablename__ = "sys_field_catalog_snapshot"
    __table_args__ = (
        _digest("catalog_digest"),
        _digest("action_catalog_digest"),
        _digest("adapter_digest"),
        CheckConstraint("schema_version = 2", name="ck_gfp_catalog_schema"),
        CheckConstraint(
            "btrim(catalog_revision) <> ''", name="ck_gfp_catalog_revision"
        ),
        CheckConstraint(
            "jsonb_typeof(canonical_document) = 'object' AND octet_length(canonical_document::text) <= 2097152",
            name="ck_gfp_catalog_budget",
        ),
    )
    catalog_digest: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    catalog_revision: Mapped[str] = mapped_column(String(64))
    action_catalog_digest: Mapped[str] = mapped_column(CHAR(64))
    adapter_digest: Mapped[str] = mapped_column(CHAR(64))
    canonical_document: Mapped[dict] = mapped_column(JSONB)


class FieldCatalogObject(Base):
    __tablename__ = "sys_field_catalog_object"
    __table_args__ = (
        UniqueConstraint(
            "catalog_digest",
            "object_id",
            "domain_kind",
            name="uq_gfp_catalog_object_domain",
        ),
        CheckConstraint(
            "domain_kind IN ('tenant','platform','system','public')",
            name="ck_gfp_catalog_domain",
        ),
        CheckConstraint(
            "object_id ~ '^[A-Za-z_][A-Za-z0-9_.:/-]{0,95}$' AND btrim(adapter_version) <> ''",
            name="ck_gfp_object_name",
        ),
        _digest("schema_digest"),
    )
    catalog_digest: Mapped[str] = mapped_column(
        CHAR(64),
        ForeignKey("sys_field_catalog_snapshot.catalog_digest", ondelete="RESTRICT"),
        primary_key=True,
    )
    object_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    domain_kind: Mapped[str] = mapped_column(String(16))
    schema_digest: Mapped[str] = mapped_column(CHAR(64))
    adapter_version: Mapped[str] = mapped_column(String(128))


class FieldPolicyHead(_Created, Base):
    __tablename__ = "sys_field_policy_head"
    __table_args__ = (
        _fk(("tenant_id",), "sys_user_field_policy_state"),
        _fk(
            ("tenant_id", "active_manifest_id", "active_manifest_digest"),
            "sys_field_policy_manifest",
            ("tenant_id", "manifest_id", "digest"),
            name="fk_gfp_head_active",
            alter=True,
        ),
        _fk(
            ("tenant_id", "candidate_manifest_id", "candidate_manifest_digest"),
            "sys_field_policy_manifest",
            ("tenant_id", "manifest_id", "digest"),
            name="fk_gfp_head_candidate",
            alter=True,
        ),
        CheckConstraint(
            "tenant_id >= 0 AND head_version >= 1", name="ck_gfp_head_identity"
        ),
        CheckConstraint(
            "rollout_state IN ('preparing','enforcing','frozen')",
            name="ck_gfp_head_state",
        ),
        CheckConstraint(
            "(active_manifest_id IS NULL) = (active_manifest_digest IS NULL) AND (candidate_manifest_id IS NULL) = (candidate_manifest_digest IS NULL)",
            name="ck_gfp_head_pairs",
        ),
        CheckConstraint(
            "(rollout_state = 'preparing' AND NOT ever_enforced AND active_manifest_id IS NULL) OR (rollout_state IN ('enforcing','frozen') AND ever_enforced AND active_manifest_id IS NOT NULL)",
            name="ck_gfp_head_enforced",
        ),
        CheckConstraint(
            "(ready_manifest_digest IS NULL AND qualification_digest IS NULL AND ready_g_epoch IS NULL AND ready_b_epoch IS NULL AND ready_until IS NULL) OR (ready_manifest_digest IS NOT NULL AND qualification_digest IS NOT NULL AND ready_g_epoch IS NOT NULL AND ready_b_epoch IS NOT NULL AND ready_until IS NOT NULL AND candidate_manifest_digest IS NOT NULL AND ready_manifest_digest = candidate_manifest_digest AND ready_g_epoch >= 1 AND ready_b_epoch >= 1)",
            name="ck_gfp_head_ready",
        ),
        _digest("ready_manifest_digest", nullable=True),
        _digest("qualification_digest", nullable=True),
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_tenant.tenant_id", ondelete="RESTRICT"),
        primary_key=True,
        autoincrement=False,
    )
    catalog_digest: Mapped[str] = mapped_column(
        CHAR(64),
        ForeignKey("sys_field_catalog_snapshot.catalog_digest", ondelete="RESTRICT"),
    )
    head_version: Mapped[int] = mapped_column(BigInteger, server_default="1")
    rollout_state: Mapped[str] = mapped_column(String(16), server_default="preparing")
    ever_enforced: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    active_manifest_id: Mapped[int | None] = mapped_column(BigInteger)
    active_manifest_digest: Mapped[str | None] = mapped_column(CHAR(64))
    candidate_manifest_id: Mapped[int | None] = mapped_column(BigInteger)
    candidate_manifest_digest: Mapped[str | None] = mapped_column(CHAR(64))
    ready_manifest_digest: Mapped[str | None] = mapped_column(CHAR(64))
    qualification_digest: Mapped[str | None] = mapped_column(CHAR(64))
    ready_g_epoch: Mapped[int | None] = mapped_column(BigInteger)
    ready_b_epoch: Mapped[int | None] = mapped_column(BigInteger)
    ready_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FieldPolicyObjectState(Base):
    __tablename__ = "sys_field_policy_object_state"
    __table_args__ = (
        _fk(("catalog_digest", "object_id", "domain_kind"), "sys_field_catalog_object"),
        CheckConstraint(
            "domain_kind = 'tenant' AND object_version >= 1",
            name="ck_gfp_object_identity",
        ),
        CheckConstraint(
            "mode IN ('legacy','ready','enforcing','frozen')", name="ck_gfp_object_mode"
        ),
        CheckConstraint(
            "(mode IN ('legacy','ready') AND NOT ever_enforced AND active_object_digest IS NULL) OR (mode IN ('enforcing','frozen') AND ever_enforced AND active_object_digest IS NOT NULL)",
            name="ck_gfp_object_enforced",
        ),
        _digest("active_object_digest", nullable=True),
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_field_policy_head.tenant_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    object_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    catalog_digest: Mapped[str] = mapped_column(CHAR(64))
    domain_kind: Mapped[str] = mapped_column(String(16), server_default="tenant")
    mode: Mapped[str] = mapped_column(String(16), server_default="legacy")
    ever_enforced: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    object_version: Mapped[int] = mapped_column(BigInteger, server_default="1")
    active_object_digest: Mapped[str | None] = mapped_column(CHAR(64))


class FieldPolicyRevision(_Created, Base):
    __tablename__ = "sys_field_policy_revision"
    __table_args__ = (
        _fk(("tenant_id", "role_id"), "sys_role"),
        _fk(("tenant_id", "object_id"), "sys_field_policy_object_state"),
        _fk(("catalog_digest", "object_id", "domain_kind"), "sys_field_catalog_object"),
        UniqueConstraint(
            "tenant_id",
            "role_id",
            "object_id",
            "revision_no",
            name="uq_gfp_revision_number",
        ),
        UniqueConstraint(
            "tenant_id",
            "role_id",
            "object_id",
            "revision_id",
            name="uq_gfp_revision_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "role_id",
            "object_id",
            "revision_id",
            "catalog_digest",
            "rules_digest",
            name="uq_gfp_revision_content",
        ),
        CheckConstraint(
            "revision_id > 0 AND role_id > 0 AND revision_no >= 1 AND actor_user_id > 0 AND domain_kind = 'tenant'",
            name="ck_gfp_revision_identity",
        ),
        CheckConstraint(
            "CASE WHEN jsonb_typeof(rules) = 'array' THEN jsonb_array_length(rules) <= CASE WHEN object_id = 'system.user' THEN 512 ELSE 2048 END ELSE false END AND octet_length(rules::text) <= 524288",
            name="ck_gfp_revision_budget",
        ),
        CheckConstraint(
            "(source_digest IS NULL AND template_revision IS NULL) OR (source_digest IS NOT NULL AND template_revision IS NOT NULL AND btrim(template_revision) <> '')",
            name="ck_gfp_revision_source",
        ),
        _digest("rules_digest"),
        _digest("source_digest", nullable=True),
    )
    revision_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger)
    role_id: Mapped[int] = mapped_column(BigInteger)
    object_id: Mapped[str] = mapped_column(String(96))
    revision_no: Mapped[int] = mapped_column(BigInteger)
    catalog_digest: Mapped[str] = mapped_column(CHAR(64))
    domain_kind: Mapped[str] = mapped_column(String(16), server_default="tenant")
    rules: Mapped[list] = mapped_column(JSONB)
    rules_digest: Mapped[str] = mapped_column(CHAR(64))
    source_digest: Mapped[str | None] = mapped_column(CHAR(64))
    template_revision: Mapped[str | None] = mapped_column(String(64))
    actor_user_id: Mapped[int] = mapped_column(BigInteger)


class FieldPolicyBinding(Base):
    __tablename__ = "sys_field_policy_binding"
    __table_args__ = (
        _fk(
            ("tenant_id", "role_id", "object_id", "revision_id"),
            "sys_field_policy_revision",
        ),
        CheckConstraint("binding_version >= 1", name="ck_gfp_binding_version"),
    )
    tenant_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    object_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    revision_id: Mapped[int] = mapped_column(BigInteger)
    binding_version: Mapped[int] = mapped_column(BigInteger)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FieldPolicyManifest(_Created, Base):
    __tablename__ = "sys_field_policy_manifest"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "manifest_id", "digest", name="uq_gfp_manifest_identity"
        ),
        UniqueConstraint(
            "tenant_id", "manifest_id", "catalog_digest", name="uq_gfp_manifest_catalog"
        ),
        UniqueConstraint(
            "tenant_id", "manifest_version", name="uq_gfp_manifest_version"
        ),
        CheckConstraint(
            "manifest_id > 0 AND manifest_version >= 1 AND expected_item_count BETWEEN 0 AND 16384 AND ((creation_kind = 'actor' AND created_by IS NOT NULL AND created_by > 0) OR (creation_kind = 'migration' AND created_by IS NULL))",
            name="ck_gfp_manifest_identity",
        ),
        CheckConstraint(
            "status IN ('building','sealed')", name="ck_gfp_manifest_status"
        ),
        _digest("digest"),
        _digest("base_active_digest", nullable=True),
        _digest("action_catalog_digest"),
        _digest("adapter_digest"),
    )
    manifest_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sys_field_policy_head.tenant_id", ondelete="RESTRICT")
    )
    manifest_version: Mapped[int] = mapped_column(BigInteger)
    base_active_digest: Mapped[str | None] = mapped_column(CHAR(64))
    catalog_digest: Mapped[str] = mapped_column(
        CHAR(64),
        ForeignKey("sys_field_catalog_snapshot.catalog_digest", ondelete="RESTRICT"),
    )
    action_catalog_digest: Mapped[str] = mapped_column(CHAR(64))
    adapter_digest: Mapped[str] = mapped_column(CHAR(64))
    digest: Mapped[str] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(String(16), server_default="building")
    expected_item_count: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    creation_kind: Mapped[str] = mapped_column(String(16), server_default="actor")


class FieldPolicyManifestItem(Base):
    __tablename__ = "sys_field_policy_manifest_item"
    __table_args__ = (
        _fk(
            ("tenant_id", "manifest_id", "catalog_digest"), "sys_field_policy_manifest"
        ),
        _fk(
            (
                "tenant_id",
                "role_id",
                "object_id",
                "revision_id",
                "catalog_digest",
                "rules_digest",
            ),
            "sys_field_policy_revision",
        ),
        Index("ix_gfp_item_tenant_object_role", "tenant_id", "object_id", "role_id"),
    )
    manifest_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    object_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(BigInteger)
    revision_id: Mapped[int] = mapped_column(BigInteger)
    catalog_digest: Mapped[str] = mapped_column(CHAR(64))
    rules_digest: Mapped[str] = mapped_column(CHAR(64))


class FieldPolicyAudit(_Created, Base):
    __tablename__ = "sys_field_policy_audit"
    __table_args__ = (
        CheckConstraint(
            "audit_id > 0 AND actor_user_id > 0 AND btrim(reason) <> '' AND before_version >= 0 AND after_version > before_version AND g_epoch >= 1 AND b_epoch >= 1",
            name="ck_gfp_audit_values",
        ),
        CheckConstraint(
            "action IN ('publish','archive','build','ready','activate','freeze','migrate')",
            name="ck_gfp_audit_action",
        ),
        _digest("before_digest", nullable=True),
        _digest("after_digest", nullable=True),
        Index("ix_gfp_audit_tenant_id", "tenant_id", "audit_id"),
    )
    audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, default=next_id)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sys_field_policy_head.tenant_id", ondelete="RESTRICT")
    )
    actor_user_id: Mapped[int] = mapped_column(BigInteger)
    role_id: Mapped[int | None] = mapped_column(BigInteger)
    object_id: Mapped[str | None] = mapped_column(String(96))
    action: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(500))
    request_id: Mapped[str] = mapped_column(String(36))
    before_digest: Mapped[str | None] = mapped_column(CHAR(64))
    after_digest: Mapped[str | None] = mapped_column(CHAR(64))
    before_version: Mapped[int] = mapped_column(BigInteger)
    after_version: Mapped[int] = mapped_column(BigInteger)
    g_epoch: Mapped[int] = mapped_column(BigInteger)
    b_epoch: Mapped[int] = mapped_column(BigInteger)


class FieldPolicyRequest(_Created, Base):
    __tablename__ = "sys_field_policy_request"
    __table_args__ = (
        _fk(
            ("tenant_id", "role_id", "object_id", "revision_id"),
            "sys_field_policy_revision",
        ),
        CheckConstraint(
            "(revision_id IS NULL AND role_id IS NULL AND object_id IS NULL) OR (revision_id IS NOT NULL AND role_id IS NOT NULL AND object_id IS NOT NULL)",
            name="ck_gfp_request_revision",
        ),
        _fk(
            ("tenant_id", "manifest_id", "manifest_digest"),
            "sys_field_policy_manifest",
            ("tenant_id", "manifest_id", "digest"),
        ),
        CheckConstraint(
            "actor_user_id > 0 AND request_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'",
            name="ck_gfp_request_id",
        ),
        CheckConstraint(
            "(manifest_id IS NULL) = (manifest_digest IS NULL) AND jsonb_typeof(receipt) = 'object' AND octet_length(receipt::text) <= 16384",
            name="ck_gfp_request_receipt",
        ),
        _digest("command_digest"),
        _digest("receipt_digest"),
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_field_policy_head.tenant_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    actor_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    command_digest: Mapped[str] = mapped_column(CHAR(64))
    receipt: Mapped[dict] = mapped_column(JSONB)
    receipt_digest: Mapped[str] = mapped_column(CHAR(64))
    manifest_id: Mapped[int | None] = mapped_column(BigInteger)
    manifest_digest: Mapped[str | None] = mapped_column(CHAR(64))
    revision_id: Mapped[int | None] = mapped_column(BigInteger)
    role_id: Mapped[int | None] = mapped_column(BigInteger)
    object_id: Mapped[str | None] = mapped_column(String(96))


class FieldPolicyMigrationMap(_Created, Base):
    __tablename__ = "sys_field_policy_migration_map"
    __table_args__ = (
        CheckConstraint(
            "legacy_table IN ('revision','audit','state') AND legacy_id >= 0 AND btrim(batch_id) <> '' AND source_build ~ '^[0-9a-f]{40}$'",
            name="ck_gfp_migration_source",
        ),
        UniqueConstraint("target_kind", "target_id", name="uq_gfp_migration_target"),
        Index(
            "ix_gfp_migration_tenant_source", "tenant_id", "legacy_table", "legacy_id"
        ),
        _digest("old_digest"),
        _digest("new_digest"),
    )
    legacy_table: Mapped[str] = mapped_column(String(16), primary_key=True)
    legacy_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sys_field_policy_head.tenant_id", ondelete="RESTRICT")
    )
    role_id: Mapped[int | None] = mapped_column(BigInteger)
    target_kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[int] = mapped_column(BigInteger)
    old_digest: Mapped[str] = mapped_column(CHAR(64))
    new_digest: Mapped[str] = mapped_column(CHAR(64))
    batch_id: Mapped[str] = mapped_column(String(64))
    source_build: Mapped[str] = mapped_column(CHAR(40))
    catalog_digest: Mapped[str] = mapped_column(
        CHAR(64),
        ForeignKey("sys_field_catalog_snapshot.catalog_digest", ondelete="RESTRICT"),
    )
