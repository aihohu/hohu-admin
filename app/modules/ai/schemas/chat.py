from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ChatRequest(BaseModel):
    """对话请求"""

    conversation_id: int = Field(..., description="会话ID")
    message: str = Field(..., min_length=1, description="用户消息")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
