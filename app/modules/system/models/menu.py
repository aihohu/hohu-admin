from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.id_generator import next_id
from app.db.base import Base, role_menus

if TYPE_CHECKING:
    from .role import Role


class Menu(Base):
    __tablename__ = "sys_menu"

    menu_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="菜单ID"
    )
    parent_id: Mapped[int] = mapped_column(
        BigInteger, nullable=True, comment="父菜单ID"
    )
    menu_name: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="菜单标题"
    )
    menu_type: Mapped[str] = mapped_column(
        String(1), default="C", comment="类型: M目录, C菜单, F按钮"
    )
    icon: Mapped[str] = mapped_column(String(50), nullable=True, comment="菜单图标")
    icon_type: Mapped[str] = mapped_column(
        String(1), nullable=True, comment="菜单图标类型"
    )
    path: Mapped[str] = mapped_column(String(255), nullable=True, comment="路由路径")
    component: Mapped[str] = mapped_column(String(255), nullable=True, comment="组件")
    route_name: Mapped[str] = mapped_column(
        String(50), nullable=True, comment="前端路由名称（name）"
    )
    route_path: Mapped[str] = mapped_column(
        String(255), nullable=True, comment="前端路由路径"
    )

    order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="排序（越小越靠前）"
    )
    status = mapped_column(String(2), nullable=False, comment="状态：1-启用，2-禁用")

    create_by: Mapped[str] = mapped_column(String(32), nullable=True, comment="创建人")
    update_by: Mapped[str] = mapped_column(String(32), nullable=True, comment="更新人")
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )

    path_param: Mapped[str] = mapped_column(
        String(255), nullable=True, comment="路径参数"
    )
    page: Mapped[str] = mapped_column(String(255), nullable=True, comment="页面组件")
    layout: Mapped[str] = mapped_column(String(255), nullable=True, comment="布局组件")
    i18n_key: Mapped[str] = mapped_column(
        String(100), nullable=True, comment="国际化key"
    )
    keep_alive: Mapped[bool] = mapped_column(Boolean, nullable=True, comment="缓存路由")
    constant: Mapped[bool] = mapped_column(Boolean, nullable=True, comment="常量路由")
    href: Mapped[str] = mapped_column(String(255), nullable=True, comment="外链")
    hide_in_menu: Mapped[bool] = mapped_column(
        Boolean, nullable=True, comment="隐藏菜单"
    )
    active_menu: Mapped[str] = mapped_column(
        String(50), nullable=True, comment="激活菜单的路由名称"
    )
    multi_tab: Mapped[bool] = mapped_column(
        Boolean, nullable=True, default=False, comment="是否支持多页签"
    )
    fixed_index_in_tab: Mapped[int] = mapped_column(
        Integer, nullable=True, comment="页签固定索引"
    )
    permission = mapped_column(String(50), nullable=True, comment="按钮/功能权限")

    query: Mapped[list] = mapped_column(JSON, nullable=True, comment="路由参数")

    roles: Mapped[list["Role"]] = relationship(
        "Role", secondary=role_menus, back_populates="menus"
    )
