import re
import uuid

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.core.exceptions import BusinessRuleException

CHAT_TRACE_PATTERN = re.compile(r"^tr_[0-9a-f]{32}$")


def resolve_chat_trace_id(value: str | None) -> str:
    """返回本次 ChatCommand 的稳定 trace ID，并拒绝模糊/短格式输入。"""
    if value is None:
        return f"tr_{uuid.uuid4().hex}"
    if not CHAT_TRACE_PATTERN.fullmatch(value):
        raise BusinessRuleException(
            "traceId 格式非法",
            error_code="AI_CHAT_TRACE_CONFLICT",
        )
    return value


class ChatRequest(BaseModel):
    """对话请求"""

    conversation_id: int = Field(..., description="会话ID")
    message: str = Field(..., min_length=1, description="用户消息")
    trace_id: str | None = Field(None, description="客户端在请求前生成的稳定 run ID")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
