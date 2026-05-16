from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class SysOperationLog(Base):
    """操作审计日志模型"""

    __tablename__ = "sys_operation_log"

    operation_log_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="日志ID"
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
        Index("ix_operation_log_create_time", "create_time"),
        Index("ix_operation_log_user_id", "user_id"),
    )
