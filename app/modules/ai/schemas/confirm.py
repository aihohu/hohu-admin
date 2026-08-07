"""HITL 确认请求 / 响应 schema"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ConfirmRequest(BaseModel):
    """POST /ai/confirm 请求体（spec §8.3）"""

    confirmation_id: str = Field(
        ..., min_length=10, description="HITL 抽屉拿到的 confirmation_id"
    )
    action: Literal["approve", "reject"] = Field(..., description="用户点确认或拒绝")

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class ConfirmResponse(BaseModel):
    """/ai/confirm 响应 data 字段（spec §8.3 + 修订 S-14）"""

    tool_call_id: str = Field(..., description="对应 ai_operation_log.tool_call_id")
    status: Literal["queued", "stream_gone"] = Field(
        default="queued",
        description=(
            "queued = 唤醒成功，业务将正常执行（前端启动 30s SSE 断流轮询兜底）；"
            "stream_gone = 流已断（服务重启 / 单 worker 切换 / SSE 已中断），"
            "tool 不会执行，前端应立即停止轮询并提示用户重新发起（修订 S-14）"
        ),
    )

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
