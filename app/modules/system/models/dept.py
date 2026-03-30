from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.id_generator import next_id
from app.db.base import Base, user_depts

if TYPE_CHECKING:
    from .user import User


class Dept(Base):
    __tablename__ = "sys_dept"

    dept_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="部门ID"
    )
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="父部门ID"
    )
    ancestors: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="祖先路径"
    )
    dept_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="部门名称"
    )
    order_num: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="显示顺序"
    )
    leader: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="负责人"
    )
    phone: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="联系电话"
    )
    email: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="邮箱"
    )
    status: Mapped[str] = mapped_column(
        String(2), nullable=False, default="1", comment="状态：1-启用，2-禁用"
    )
    create_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="创建者"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    update_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="更新者"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )

    users: Mapped[list["User"]] = relationship(
        "User", secondary=user_depts, back_populates="depts", lazy="selectin"
    )
