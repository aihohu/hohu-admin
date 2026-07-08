from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, func
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

    log_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="日志ID"
    )
    trace_id: Mapped[str] = mapped_column(
        String(64), comment="追踪ID，串联同对话多 tool"
    )
    conversation_id: Mapped[int] = mapped_column(BigInteger, comment="会话ID")
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
    started_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="单次 tool 调用开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
