from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class DataScopeDemo(Base):
    """数据权限演示业务表。

    字段契约（与 app/utils/data_scope.py 对齐）：
    - dept_id：部门 ID（BigInteger），DEPT/CUSTOM/DEPT_AND_SUB scope 的过滤锚点。
    - create_by：创建人 user_id（BigInteger，不是 user_name 字符串），
      SELF scope 据此过滤。注意与 sys_dept/sys_role 等表用 String(32) 存
      user_name 的惯例不同——这里刻意用 BigInteger 存 ID，让 data_scope
      的 `user_col == user.user_id` 直接可比。
    """

    __tablename__ = "sys_data_scope_demo"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "demo_id", name="uq_sys_data_scope_demo_tenant_demo_id"
        ),
        ForeignKeyConstraint(
            ("tenant_id", "dept_id"),
            ("sys_dept.tenant_id", "sys_dept.dept_id"),
            name="fk_sys_data_scope_demo_tenant_dept",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "create_by"),
            ("sys_user.tenant_id", "sys_user.user_id"),
            name="fk_sys_data_scope_demo_tenant_creator",
            ondelete="RESTRICT",
        ),
        Index("ix_sys_data_scope_demo_tenant_dept", "tenant_id", "dept_id"),
        Index("ix_sys_data_scope_demo_tenant_creator", "tenant_id", "create_by"),
    )

    demo_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="演示数据ID"
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_tenant.tenant_id", ondelete="RESTRICT"),
        nullable=False,
        comment="租户ID；必须由可信 TenantContext 显式写入",
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False, comment="标题")
    content: Mapped[str | None] = mapped_column(Text, nullable=True, comment="内容")
    dept_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属部门ID（数据权限锚点）"
    )
    create_by: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="创建人 user_id（SELF scope 锚点，存 ID 而非 user_name）",
    )
    status: Mapped[str] = mapped_column(
        String(2), nullable=False, default="1", comment="状态：1-启用，2-禁用"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )
