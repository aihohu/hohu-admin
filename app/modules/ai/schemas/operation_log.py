"""AI 操作日志 schema — spec §9.3 SSE 断流兜底查询"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


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
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )
