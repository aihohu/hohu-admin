from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.id_generator import next_id
from app.db.base import Base, role_depts, role_menus, user_roles

if TYPE_CHECKING:
    from .dept import Dept
    from .menu import Menu
    from .user import User


class Role(Base):
    __tablename__ = "sys_role"

    role_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="角色ID"
    )
    role_name: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, comment="角色名称"
    )
    role_code: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False, comment="角色编码"
    )
    role_desc: Mapped[str] = mapped_column(
        String(255), nullable=True, comment="角色描述"
    )
    data_scope: Mapped[str] = mapped_column(
        String(2),
        nullable=False,
        default="1",
        comment="数据权限范围：1-全部，2-自定义，3-本部门，4-本部门及以下，5-仅本人",
    )
    status: Mapped[str] = mapped_column(
        String(2), nullable=False, comment="状态：1-启用，2-禁用"
    )
    create_by: Mapped[str] = mapped_column(String(32), nullable=True, comment="创建人")
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    update_by: Mapped[str] = mapped_column(String(64), nullable=True, comment="更新人")
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )

    users: Mapped[list["User"]] = relationship(
        "User", secondary=user_roles, back_populates="roles"
    )
    menus: Mapped[list["Menu"]] = relationship(
        "Menu", secondary=role_menus, back_populates="roles", lazy="selectin"
    )
    depts: Mapped[list["Dept"]] = relationship(
        "Dept", secondary=role_depts, lazy="selectin"
    )
