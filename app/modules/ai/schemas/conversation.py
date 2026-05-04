from datetime import datetime

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


class ConversationQuery(BaseModel):
    """会话查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(20, ge=1, le=100, description="每页数量")
    title: str | None = Field(None, description="标题（模糊查询）")
    status: int | None = Field(None, description="状态")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
