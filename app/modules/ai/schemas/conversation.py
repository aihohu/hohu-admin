from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel

from app.core.config import settings


class ConversationCreate(BaseModel):
    """会话创建请求"""

    title: str | None = Field("新对话", max_length=200, description="会话标题")
    model_name: str | None = Field(None, max_length=100, description="模型标识")
    system_prompt: str | None = Field(None, description="系统提示词")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ConversationUpdate(BaseModel):
    """会话更新请求"""

    title: str | None = Field(None, max_length=200, description="会话标题")
    model_name: str | None = Field(None, max_length=100, description="模型标识")
    system_prompt: str | None = Field(None, description="系统提示词")
    status: int | None = Field(None, description="状态：0=活跃, 1=归档")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ConversationOut(BaseModel):
    """会话输出"""

    conversation_id: int
    user_id: int
    title: str
    model_name: str
    system_prompt: str | None
    status: int
    create_time: datetime
    update_time: datetime

    @field_serializer("conversation_id", "user_id")
    def serialize_id(self, v: int, _info):
        return str(v)

    @field_serializer("create_time", "update_time")
    def serialize_time(self, dt: datetime, _info) -> str:
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )


class PendingActionOut(BaseModel):
    """Safe, reloadable projection of a prepared confirmation."""

    action_id: int
    confirmation_id: str
    source_user_message_id: int
    trace_id: str
    tool: str
    tool_call_id: str
    source_tool_call_id: str | None
    interaction_flow: str
    presentation: dict[str, Any]
    expires_at: datetime

    @field_serializer("action_id", "source_user_message_id")
    def serialize_pending_id(self, value: int, _info) -> str:
        return str(value)

    @field_serializer("expires_at")
    def serialize_pending_time(self, value: datetime, _info) -> str:
        return value.isoformat().replace("+00:00", "Z")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PendingActionStatusOut(BaseModel):
    """Minimal status returned when a pending presentation cannot be projected."""

    confirmation_id: str
    status: str
    error_code: str = "AI_RESULT_PROJECTION_FORBIDDEN"
    finished_at: datetime | None = None

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ConversationQuery(BaseModel):
    """会话查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(20, ge=1, le=100, description="每页数量")
    title: str | None = Field(None, description="标题（模糊查询）")
    status: int | None = Field(None, description="状态")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
