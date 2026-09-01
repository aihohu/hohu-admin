from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
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
        CheckConstraint("row_version >= 1", name="ck_sys_tenant_row_version"),
        UniqueConstraint("tenant_code", name="uq_sys_tenant_tenant_code"),
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
    row_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
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
