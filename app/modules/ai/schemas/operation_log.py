"""Safe owner status and tenant-scoped AI Trace response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel

from app.schemas.types import LocalNaiveDatetime


class TraceListQuery(BaseModel):
    """Filters for the grouped tenant Trace list."""

    current: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)
    trace_id: str | None = Field(None, min_length=1, max_length=64)
    actor_id: int | None = Field(None, gt=0)
    agent_code: str | None = Field(None, min_length=1, max_length=64)
    tool_name: str | None = Field(None, min_length=1, max_length=128)
    status: str | None = Field(None, min_length=1, max_length=32)
    queued_from: LocalNaiveDatetime | None = None
    queued_to: LocalNaiveDatetime | None = None

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class TraceTargetOut(BaseModel):
    """One allowlisted stable audit target."""

    type: str
    id: str

    model_config = ConfigDict(extra="forbid")


class TraceSummaryOut(BaseModel):
    """One trace-group row for the audit list."""

    trace_id: str
    actor_id: int
    actor_name: str
    agent_codes: list[str]
    tool_names: list[str]
    statuses: list[str]
    operation_count: int
    queued_at: datetime
    finished_at: datetime | None = None

    @field_serializer("actor_id")
    def _serialize_actor_id(self, value: int) -> str:
        return str(value)

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class TraceOperationOut(BaseModel):
    """Strict allowlist for one operation inside a Trace detail."""

    log_id: int
    tool_call_id: str
    tool_name: str
    agent_code: str
    actor_id: int
    actor_name: str | None = None
    source_message_id: int | None = None
    source_message_role: str | None = None
    source_message_at: datetime | None = None
    target_summary: list[TraceTargetOut] = Field(default_factory=list)
    execution_mode: str
    risk_level: str
    status: str
    error_code: str | None = None
    confirmation_id: str | None = None
    approved_by: int | None = None
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    hitl_wait_ms: int | None = None

    @field_serializer(
        "log_id",
        "actor_id",
        "source_message_id",
        "approved_by",
        when_used="unless-none",
    )
    def _serialize_ids(self, value: int) -> str:
        return str(value)

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


class TraceDetailOut(BaseModel):
    """Tenant-scoped Trace detail without message or argument content."""

    trace_id: str
    conversation_id: int
    operations: list[TraceOperationOut]

    @field_serializer("conversation_id")
    def _serialize_conversation_id(self, value: int) -> str:
        return str(value)

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class OperationLogOut(BaseModel):
    """GET /ai/operation-log?tool_call_id=... 响应

    只暴露审计元信息，不返回 args_summary 或 result_summary 明细，避免泄漏。
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


class OperationLogStatusOut(BaseModel):
    """owner 失去聊天入口权限后的最小轮询状态。"""

    tool_call_id: str
    status: str
    error_code: str | None = None
    finished_at: datetime | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )
