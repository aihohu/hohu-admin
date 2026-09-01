from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Table,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# 用户-角色 关联表
user_roles = Table(
    "sys_user_role",
    Base.metadata,
    Column(
        "tenant_id",
        BigInteger,
        nullable=False,
        comment="租户ID；必须由可信 TenantContext 显式写入",
    ),
    Column("user_id", BigInteger, nullable=False),
    Column("role_id", BigInteger, nullable=False),
    PrimaryKeyConstraint("tenant_id", "user_id", "role_id"),
    ForeignKeyConstraint(
        ("tenant_id", "user_id"),
        ("sys_user.tenant_id", "sys_user.user_id"),
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ("tenant_id", "role_id"),
        ("sys_role.tenant_id", "sys_role.role_id"),
        ondelete="CASCADE",
    ),
    Index("ix_sys_user_role_tenant_role", "tenant_id", "role_id"),
)


# 角色-菜单 关联表
role_menus = Table(
    "sys_role_menu",
    Base.metadata,
    Column(
        "tenant_id",
        BigInteger,
        nullable=False,
        comment="租户ID；必须由可信 TenantContext 显式写入",
    ),
    Column("role_id", BigInteger, nullable=False),
    Column("menu_id", BigInteger, nullable=False),
    PrimaryKeyConstraint("tenant_id", "role_id", "menu_id"),
    ForeignKeyConstraint(
        ("tenant_id", "role_id"),
        ("sys_role.tenant_id", "sys_role.role_id"),
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ("tenant_id", "menu_id"),
        ("sys_menu.tenant_id", "sys_menu.menu_id"),
        ondelete="CASCADE",
    ),
    Index("ix_sys_role_menu_tenant_menu", "tenant_id", "menu_id"),
)


# 角色-部门 关联表（数据权限：自定义数据范围时指定部门）
role_depts = Table(
    "sys_role_dept",
    Base.metadata,
    Column(
        "tenant_id",
        BigInteger,
        nullable=False,
        comment="租户ID；必须由可信 TenantContext 显式写入",
    ),
    Column("role_id", BigInteger, nullable=False),
    Column("dept_id", BigInteger, nullable=False),
    PrimaryKeyConstraint("tenant_id", "role_id", "dept_id"),
    ForeignKeyConstraint(
        ("tenant_id", "role_id"),
        ("sys_role.tenant_id", "sys_role.role_id"),
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ("tenant_id", "dept_id"),
        ("sys_dept.tenant_id", "sys_dept.dept_id"),
        ondelete="CASCADE",
    ),
    Index("ix_sys_role_dept_tenant_dept", "tenant_id", "dept_id"),
)


# 用户-部门 关联表
user_depts = Table(
    "sys_user_dept",
    Base.metadata,
    Column(
        "tenant_id",
        BigInteger,
        nullable=False,
        comment="租户ID；必须由可信 TenantContext 显式写入",
    ),
    Column("user_id", BigInteger, nullable=False),
    Column("dept_id", BigInteger, nullable=False),
    Column("is_primary", String(2), nullable=False, default="N"),
    PrimaryKeyConstraint("tenant_id", "user_id", "dept_id"),
    ForeignKeyConstraint(
        ("tenant_id", "user_id"),
        ("sys_user.tenant_id", "sys_user.user_id"),
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ("tenant_id", "dept_id"),
        ("sys_dept.tenant_id", "sys_dept.dept_id"),
        ondelete="CASCADE",
    ),
    Index("ix_sys_user_dept_tenant_dept", "tenant_id", "dept_id"),
)
