"""供 SSE 断流兜底查询使用的 AI 操作日志 schema。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


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
