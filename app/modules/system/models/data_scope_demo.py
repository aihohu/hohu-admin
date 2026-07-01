from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
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

    demo_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="演示数据ID"
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
