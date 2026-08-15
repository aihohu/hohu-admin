from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel

from app.core.config import settings


class ModelCreate(BaseModel):
    """模型创建请求"""

    name: str = Field(..., min_length=1, max_length=100, description="模型名称")
    capabilities: list[str] = Field(
        ..., description="能力标签，如 ['text','vision','image-gen']"
    )
    base_url: str | None = Field(None, max_length=500, description="模型级 API 地址")
    is_enabled: bool = Field(True, description="是否启用")
    sort_order: int = Field(0, description="排序")
    config: dict | None = Field(None, description="扩展配置")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ModelUpdate(BaseModel):
    """模型更新请求"""

    name: str | None = Field(None, max_length=100, description="模型名称")
    capabilities: list[str] | None = Field(None, description="能力标签")
    base_url: str | None = Field(None, max_length=500, description="模型级 API 地址")
    is_enabled: bool | None = Field(None, description="是否启用")
    sort_order: int | None = Field(None, description="排序")
    config: dict | None = Field(None, description="扩展配置")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ModelOut(BaseModel):
    """模型输出"""

    model_id: int
    provider_id: int
    name: str
    capabilities: list[str]
    base_url: str | None
    is_enabled: bool
    sort_order: int
    config: dict | None
    create_by: str | None
    create_time: datetime
    egress_status: str | None = None

    @field_serializer("model_id", "provider_id")
    def serialize_id(self, v: int, _info):
        return str(v)

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )


class ModelOption(BaseModel):
    """聊天与 Agent 配置共用的模型安全投影。"""

    model_id: int
    label: str
    provider_code: str
    capabilities: list[str]

    @field_serializer("model_id")
    def serialize_model_id(self, value: int) -> str:
        return str(value)

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
