from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    FetchedValue,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.id_generator import next_id
from app.db.base import Base

if TYPE_CHECKING:
    from .user import User


class Tenant(Base):
    """Platform-global registry for tenant authentication boundaries."""

    __tablename__ = "sys_tenant"
    __table_args__ = (
        CheckConstraint("status IN ('1', '2')", name="ck_sys_tenant_status"),
        CheckConstraint("tenant_id >= 0", name="ck_sys_tenant_nonnegative_id"),
        CheckConstraint(
            "tenant_code ~ '^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$'",
            name="ck_sys_tenant_code_format",
        ),
        CheckConstraint(
            "btrim(tenant_name) <> '' AND tenant_name !~ '[[:cntrl:]]'",
            name="ck_sys_tenant_name_format",
        ),
        CheckConstraint("row_version >= 1", name="ck_sys_tenant_row_version"),
        CheckConstraint(
            "(lifecycle_state = 'active' AND status = '1') OR "
            "(lifecycle_state IN ('prepared', 'disabled') AND status = '2')",
            name="ck_sys_tenant_lifecycle_status",
        ),
        CheckConstraint(
            "(provisioning_key_hash IS NULL AND provisioning_fingerprint IS NULL) "
            "OR (provisioning_key_hash IS NOT NULL "
            "AND provisioning_fingerprint IS NOT NULL "
            "AND provisioning_key_hash ~ '^[0-9a-f]{64}$' "
            "AND provisioning_fingerprint ~ '^[0-9a-f]{64}$')",
            name="ck_sys_tenant_provisioning_hashes",
        ),
        CheckConstraint(
            "(tenant_id = 0 AND bootstrap_version = 1 "
            "AND bootstrap_key_hash IS NULL AND bootstrap_fingerprint IS NULL) OR "
            "(tenant_id <> 0 AND bootstrap_version = 0 "
            "AND bootstrap_key_hash IS NULL AND bootstrap_fingerprint IS NULL) OR "
            "(tenant_id <> 0 AND bootstrap_version = 1 "
            "AND bootstrap_key_hash IS NOT NULL "
            "AND bootstrap_fingerprint IS NOT NULL "
            "AND bootstrap_key_hash ~ '^[0-9a-f]{64}$' "
            "AND bootstrap_fingerprint ~ '^[0-9a-f]{64}$')",
            name="ck_sys_tenant_bootstrap_state",
        ),
        CheckConstraint(
            "lifecycle_state <> 'active' OR bootstrap_version >= 1",
            name="ck_sys_tenant_active_bootstrapped",
        ),
        UniqueConstraint("tenant_code", name="uq_sys_tenant_tenant_code"),
        UniqueConstraint(
            "provisioning_key_hash", name="uq_sys_tenant_provisioning_key_hash"
        ),
        UniqueConstraint("bootstrap_key_hash", name="uq_sys_tenant_bootstrap_key_hash"),
    )

    tenant_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="租户ID"
    )
    tenant_code: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="稳定的小写租户代码"
    )
    tenant_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="租户展示名称"
    )
    status: Mapped[str] = mapped_column(
        String(2), nullable=False, default="1", server_default="1", comment="状态"
    )
    lifecycle_state: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="active/prepared/disabled"
    )
    provisioning_key_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="tenant prepare 幂等键 SHA-256"
    )
    provisioning_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="tenant prepare 规范化请求 SHA-256"
    )
    bootstrap_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="租户原子引导版本；0=未引导，1=Plan 5-B-B 完成",
    )
    bootstrap_key_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="tenant bootstrap 幂等键 SHA-256"
    )
    bootstrap_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="tenant bootstrap 请求 keyed-HMAC fingerprint",
    )
    row_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        server_onupdate=FetchedValue(),
        comment="授权与缓存漂移检测版本",
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

    users: Mapped[list["User"]] = relationship("User", back_populates="tenant")
