from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class SysJob(Base):
    """定时任务配置模型"""

    __tablename__ = "sys_job"

    job_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="任务ID"
    )
    job_name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="任务名称"
    )
    job_key: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, comment="任务标识"
    )
    cron_expression: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="cron表达式"
    )
    trigger_type: Mapped[str] = mapped_column(
        String(10), default="cron", comment="调度类型：cron-表达式，interval-间隔"
    )
    interval_value: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="间隔值"
    )
    interval_unit: Mapped[str | None] = mapped_column(
        String(10), nullable=True, comment="间隔单位：seconds/minutes/hours/days"
    )
    job_args: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="任务参数JSON"
    )
    status: Mapped[str] = mapped_column(
        String(2), default="1", comment="状态：1-启用，2-停用"
    )
    concurrent: Mapped[str] = mapped_column(
        String(2), default="2", comment="并发策略：1-允许，2-不允许"
    )
    timeout_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="单次执行超时秒数（空表示不限）"
    )
    max_retries: Mapped[int] = mapped_column(
        Integer, default=0, comment="失败重试次数（0 表示不重试）"
    )
    run_on_enable: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="启用时是否立即执行一次"
    )
    remark: Mapped[str | None] = mapped_column(
        String(256), nullable=True, comment="备注"
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
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )


class SysJobLog(Base):
    """定时任务执行日志模型"""

    __tablename__ = "sys_job_log"
    __table_args__ = (
        Index("ix_sys_job_log_status_start_time", "status", "start_time"),
    )

    job_log_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="日志ID"
    )
    job_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="任务ID")
    job_name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="任务名称"
    )
    job_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="任务标识")
    status: Mapped[str] = mapped_column(
        String(2), default="3", comment="状态：1-成功，2-失败，3-执行中"
    )
    error_msg: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="异常信息"
    )
    start_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="开始时间"
    )
    end_time: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="结束时间"
    )
    duration: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="耗时（毫秒）"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=1, comment="本次触发实际执行次数（含重试）"
    )
    runner_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="写入此日志的执行进程标识（uuid4，孤儿守护用）",
    )
