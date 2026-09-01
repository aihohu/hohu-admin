from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.id_generator import next_id
from app.db.base import Base, user_depts, user_roles

if TYPE_CHECKING:
    from .dept import Dept
    from .role import Role
    from .tenant import Tenant


class User(Base):
    __tablename__ = "sys_user"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_sys_user_tenant_user_id"),
        UniqueConstraint("tenant_id", "user_name", name="uq_sys_user_tenant_user_name"),
        UniqueConstraint(
            "tenant_id", "employee_no", name="uq_sys_user_tenant_employee_no"
        ),
        Index("ix_sys_user_tenant_status", "tenant_id", "status"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="用户ID"
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_tenant.tenant_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="租户ID；必须由可信 TenantContext 显式写入",
    )
    user_name: Mapped[str] = mapped_column(String(50), nullable=False, comment="账号")
    employee_no: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="员工工号，用于企业同步、LDAP 或 ERP 对接；UNIQUE 但允许多个 NULL",
    )
    nickname: Mapped[str] = mapped_column(String(50), nullable=True, comment="昵称")
    hashed_password: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="加密密码"
    )
    status: Mapped[str] = mapped_column(String(10), default="1", comment="状态")

    user_avatar: Mapped[str] = mapped_column(
        String(255), nullable=True, comment="头像地址"
    )
    user_email: Mapped[str] = mapped_column(String(100), nullable=True, comment="邮箱")
    user_phone: Mapped[str] = mapped_column(String(20), nullable=True, comment="手机号")
    user_gender: Mapped[str] = mapped_column(
        String(1), nullable=True, comment="用户性别: 0:未知,1:男,2:女"
    )

    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )

    roles: Mapped[list["Role"]] = relationship(
        "Role", secondary=user_roles, back_populates="users", lazy="selectin"
    )

    depts: Mapped[list["Dept"]] = relationship(
        "Dept", secondary=user_depts, back_populates="users", lazy="selectin"
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="users")
