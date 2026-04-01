from sqlalchemy import BigInteger, Column, ForeignKey, String, Table
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# 用户-角色 关联表
user_roles = Table(
    "sys_user_role",
    Base.metadata,
    Column(
        "user_id",
        BigInteger,
        ForeignKey("sys_user.user_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "role_id",
        BigInteger,
        ForeignKey("sys_role.role_id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


# 角色-菜单 关联表
role_menus = Table(
    "sys_role_menu",
    Base.metadata,
    Column(
        "role_id",
        BigInteger,
        ForeignKey("sys_role.role_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "menu_id",
        BigInteger,
        ForeignKey("sys_menu.menu_id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


# 用户-部门 关联表
user_depts = Table(
    "sys_user_dept",
    Base.metadata,
    Column(
        "user_id",
        BigInteger,
        ForeignKey("sys_user.user_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "dept_id",
        BigInteger,
        ForeignKey("sys_dept.dept_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("is_primary", String(2), nullable=False, default="N"),
)
