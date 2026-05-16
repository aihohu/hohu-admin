from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class SysLoginLog(Base):
    """登录日志模型"""

    __tablename__ = "sys_login_log"

    login_log_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="日志ID"
    )
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="登录用户ID"
    )
    username: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="登录用户名"
    )
    ip: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="登录IP")
    user_agent: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="浏览器信息"
    )
    status: Mapped[str] = mapped_column(
        String(2), default="1", comment="状态：1-成功，2-失败，3-锁定"
    )
    message: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="结果描述"
    )
    login_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="登录时间"
    )

    __table_args__ = (Index("ix_login_log_login_time", "login_time"),)
