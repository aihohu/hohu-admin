from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel

from app.core.config import settings
from app.core.security import decrypt_value


class ProviderCreate(BaseModel):
    """提供商创建请求"""

    provider_code: str = Field(
        ..., min_length=1, max_length=50, description="提供商标识"
    )
    name: str = Field(..., min_length=1, max_length=100, description="显示名称")
    api_key: str = Field(..., min_length=1, max_length=500, description="API Key")
    base_url: str | None = Field(None, max_length=500, description="API 地址")
    is_enabled: bool = Field(True, description="是否启用")
    config: dict | None = Field(None, description="扩展配置")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ProviderUpdate(BaseModel):
    """提供商更新请求"""

    provider_code: str | None = Field(None, max_length=50, description="提供商标识")
    name: str | None = Field(None, max_length=100, description="显示名称")
    api_key: str | None = Field(None, max_length=500, description="API Key")
    base_url: str | None = Field(None, max_length=500, description="API 地址")
    is_enabled: bool | None = Field(None, description="是否启用")
    config: dict | None = Field(None, description="扩展配置")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ProviderOut(BaseModel):
    """提供商输出"""

    provider_id: int
    provider_code: str
    name: str
    api_key: str
    base_url: str | None
    is_enabled: bool
    config: dict | None
    create_time: datetime
    egress_status: str | None = None

    @field_serializer("provider_id")
    def serialize_id(self, v: int, _info):
        return str(v)

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        return dt.strftime(settings.DATETIME_FORMAT)

    @field_serializer("api_key")
    def mask_api_key(self, v: str, _info) -> str:
        plaintext = decrypt_value(v)
        if len(plaintext) <= 8:
            return "****"
        return plaintext[:4] + "****" + plaintext[-4:]

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )


class ProviderQuery(BaseModel):
    """提供商查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    provider_code: str | None = Field(None, description="提供商标识")
    name: str | None = Field(None, description="名称（模糊查询）")
    is_enabled: bool | None = Field(None, description="是否启用")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ProviderTestRequest(BaseModel):
    """已保存 Provider/Model 连通性测试请求。"""

    model_id: str = Field(
        ...,
        strict=True,
        pattern=r"^[1-9][0-9]*$",
        max_length=32,
    )

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class ProviderTestResult(BaseModel):
    provider_id: int
    model_id: int
    status: str = "ok"

    @field_serializer("provider_id", "model_id")
    def serialize_id(self, value: int, _info):
        return str(value)

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
