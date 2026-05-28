from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel

from app.core.config import settings


class MessageOut(BaseModel):
    """消息输出"""

    message_id: int
    conversation_id: int
    parent_message_id: int | None
    role: str
    message_type: str
    content: str | None
    parts: list[dict] | None
    tokens_input: int | None
    tokens_output: int | None
    tool_calls: dict | None
    create_time: datetime

    @field_serializer("message_id", "conversation_id", "parent_message_id")
    def serialize_id(self, v: int | None, _info):
        return str(v) if v is not None else None

    @field_serializer("create_time")
    def serialize_time(self, dt: datetime, _info) -> str:
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )


class MessageCreate(BaseModel):
    """消息创建（内部使用，非 API 暴露）"""

    conversation_id: int
    role: str = Field(..., description="角色：user / assistant / system / tool")
    content: str | None = Field(None, description="消息内容")
    parts: list[dict] | None = Field(None, description="结构化消息内容")
    message_type: str = Field(
        "text", description="类型：text / tool_call / tool_result"
    )
    parent_message_id: int | None = Field(None, description="父消息ID")
    tokens_input: int | None = Field(None, description="输入 token 数")
    tokens_output: int | None = Field(None, description="输出 token 数")
    tool_calls: dict | None = Field(None, description="工具调用记录")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
