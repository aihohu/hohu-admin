"""HITL 确认请求 / 响应 schema"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ConfirmRequest(BaseModel):
    """POST /ai/confirm 请求体（spec §8.3）"""

    confirmation_id: str = Field(
        ..., min_length=10, description="HITL 抽屉拿到的 confirmation_id"
    )
    action: Literal["approved", "rejected"] = Field(..., description="用户点确认或拒绝")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ConfirmResponse(BaseModel):
    """/ai/confirm 响应 data 字段（spec §8.3）"""

    tool_call_id: str = Field(..., description="对应 ai_operation_log.tool_call_id")
    status: Literal["queued"] = "queued"

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
