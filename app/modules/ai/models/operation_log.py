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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class AiOperationLog(Base):
    """AI 操作日志 + 安全事件（合并表）

    按 spec §4.4 / §9.1 / §9.6 设计：
    - 每次 tool 调用写一行，按 trace_id 串联同一对话的多次调用
    - 安全事件（注入命中 / Guardrail 命中）合并到 is_security_event 字段，不独立建表
    - status 含 running / pending_confirmation / success / failed / rejected / expired
    - tool_call_id 唯一索引：§8.3 SSE 断流兜底轮询端点用
    """

    __tablename__ = "ai_operation_log"
    __table_args__ = (
        Index(
            "ix_ai_operation_source_status",
            "conversation_id",
            "source_user_message_id",
            "status",
        ),
    )

    log_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="日志ID"
    )
    trace_id: Mapped[str] = mapped_column(
        String(64), comment="追踪ID，串联同对话多 tool"
    )
    conversation_id: Mapped[int] = mapped_column(BigInteger, comment="会话ID")
    source_user_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="触发 operation 的 user message；NULL 仅兼容历史数据",
    )
    readonly_snapshot: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="执行时 AiToolMeta.readonly 快照；未知按 write 处理",
    )
    user_id: Mapped[int] = mapped_column(BigInteger, comment="调用用户ID")
    tool_name: Mapped[str] = mapped_column(String(128), comment="tool 全限定名")
    tool_call_id: Mapped[str] = mapped_column(
        String(64), unique=True, comment="单次 tool 调用 ID，§8.3 兜底轮询用"
    )
    args_hash: Mapped[str] = mapped_column(
        String(64), comment="SHA256 完整 64 字符，不截断"
    )
    args_summary: Mapped[str] = mapped_column(
        Text, comment="仅元信息（tool + risk + mode + dry_run_count），不含 args 原值"
    )
    result_summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="status + affected_count + duration_ms + error_code",
    )
    risk_level: Mapped[str] = mapped_column(
        String(16), comment="low / high / destructive"
    )
    execution_mode: Mapped[str] = mapped_column(String(32), comment="autonomous / hitl")
    status: Mapped[str] = mapped_column(
        String(32),
        comment="running / pending_confirmation / success / failed / rejected / expired",
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 时间语义（spec §4.4 时间戳约定，2026-07-10 修订 S-3）：
    # - queued_at:    行级创建时间（pending_confirmation 入库时刻），含 HITL 等待之前
    # - started_at:   业务执行起点（HITL approved 后 / autonomous 入库后真正开始执行）
    # - finished_at:  业务执行终点（success / failed / rejected / expired）
    # duration_ms = finished_at - started_at（不含 HITL 等待时间）
    # hitl_wait_ms = started_at - queued_at（autonomous 流为 None）
    queued_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        comment="行级创建时间（pending_confirmation 入库时刻）",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="业务执行起点（HITL approve 后 / autonomous 入库后）",
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="业务执行耗时，不含 HITL 等待"
    )
    hitl_wait_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="HITL 等待耗时（autonomous 流为 None）"
    )
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # 安全事件字段（合并 ai_security_event，MVP 不独立建表）
    is_security_event: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否安全事件（注入命中 / Guardrail 命中）",
    )
    event_type: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="injection_pattern_matched / guardrail_keyword",
    )
    severity: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="info / warning / critical"
    )
