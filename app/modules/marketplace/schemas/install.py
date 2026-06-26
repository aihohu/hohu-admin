from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel

from app.core.config import settings


class InstallBase(BaseModel):
    """安装 Schema 公共配置"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class InstallOut(InstallBase):
    """安装记录输出"""

    id: int
    app_id: int
    app_slug: str
    app_name: str
    installed_version: str
    status: str
    config: dict[str, Any] | None = None
    installed_at: datetime
    updated_at: datetime

    @field_serializer("id", "app_id")
    def serialize_id(self, v: int) -> str:
        return str(v)

    @field_serializer("installed_at", "updated_at")
    def serialize_datetime(self, v: datetime) -> str:
        return v.strftime(settings.DATETIME_FORMAT)


class InstallCreate(BaseModel):
    """安装应用请求"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    app_slug: str
    version: str | None = None  # None = 最新已发布版本
    approved_permissions: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class InstallQuery(BaseModel):
    """安装记录查询参数"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    current: int = 1
    size: int = 10
    status: str | None = None
    app_slug: str | None = None
