"""AI 操作日志 schema — spec §9.3 SSE 断流兜底查询"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel

from app.core.config import settings


class OperationLogOut(BaseModel):
    """GET /ai/operation-log?tool_call_id=... 响应

    spec §9.3: 字段过滤——只暴露审计元信息（不含 args_summary / result_summary 详细内容，避免泄漏）
    """

    tool_call_id: str = Field(..., description="单次 tool 调用 ID")
    tool_name: str
    status: str = Field(
        ..., description="running/pending_confirmation/success/failed/rejected/expired"
    )
    error_code: str | None = None
    # started_at 在 pending_confirmation / expired / rejected 状态下可能为 NULL：
    # 业务还没真正开始执行（HITL 等待 / 未 approve / 超时未操作）。与
    # AiOperationLog.started_at: Mapped[datetime | None] 一致。
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class PendingConfirmationOut(BaseModel):
    """GET /ai/pending-confirmations 响应（spec §14 跨会话恢复）

    字段过滤：args_summary 是 spec §7.2 "仅元信息"（tool + risk + mode +
    dry_run_count），不含 args 原值。args 详情走 attemptResume → SSE
    confirmation_required 流获取，不在列表暴露（D3）。
    """

    confirmation_id: str
    tool_call_id: str
    tool_name: str
    conversation_id: int = Field(
        ..., description="Snowflake，序列化为 str 防 JS BigInt 精度"
    )
    conversation_title: str | None = Field(
        None, description="原对话标题，banner 显示上下文用；None=对话被删除"
    )
    trace_id: str
    args_summary: str
    risk_level: str
    queued_at: datetime
    expires_at: datetime = Field(
        ..., description="来自 Redis pending payload，DB 无此字段"
    )

    @field_serializer("conversation_id")
    def serialize_id(self, v: int, _info) -> str:
        return str(v)

    @field_serializer("queued_at", "expires_at")
    def serialize_time(self, dt: datetime, _info) -> str:
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )
