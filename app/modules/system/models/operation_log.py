from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class SysOperationLog(Base):
    """操作审计日志模型"""

    __tablename__ = "sys_operation_log"

    operation_log_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="日志ID"
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_tenant.tenant_id", ondelete="RESTRICT"),
        nullable=False,
        comment="租户审计归属",
    )
    audit_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="tenant", comment="审计作用域"
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="操作人ID")
    username: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="操作人用户名"
    )
    module: Mapped[str] = mapped_column(String(50), nullable=False, comment="业务模块")
    action: Mapped[str] = mapped_column(String(20), nullable=False, comment="操作类型")
    method: Mapped[str] = mapped_column(String(10), nullable=False, comment="HTTP方法")
    path: Mapped[str] = mapped_column(String(200), nullable=False, comment="请求路径")
    request_params: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="请求参数摘要"
    )
    status_code: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="响应状态码"
    )
    ip: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="操作者IP"
    )
    duration: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="耗时（毫秒）"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="操作时间"
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "operation_log_id",
            name="uq_sys_operation_log_tenant_log_id",
        ),
        CheckConstraint(
            "audit_scope IN ('tenant', 'platform')",
            name="ck_sys_operation_log_audit_scope",
        ),
        Index("ix_operation_log_tenant_time", "tenant_id", "create_time"),
        Index("ix_operation_log_tenant_user", "tenant_id", "user_id"),
    )
