from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel

from app.core.config import settings


class AppBase(BaseModel):
    """应用市场 Schema 公共配置（snake_case ↔ camelCase 自动转换）"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class AppOut(AppBase):
    """应用列表/卡片输出"""

    # 注意：内部读取 int（与模型一致），field_serializer 在输出时转为字符串
    # 这样既能 from_attributes 直接读 int，又能保证 JSON 输出为 str
    id: int
    name: str
    slug: str
    type: str
    category: str
    description: str | None = None
    icon: str | None = None
    author_name: str | None = None
    status: str
    homepage: str | None = None
    license: str | None = None
    download_count: int
    avg_rating: float
    rating_count: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("id")
    def serialize_id(self, v: int) -> str:
        return str(v)

    @field_serializer("created_at", "updated_at")
    def serialize_datetime(self, v: datetime) -> str:
        return v.strftime(settings.DATETIME_FORMAT)


class AppDetailOut(AppOut):
    """应用详情（含当前发布版本、标签等扩展字段）"""

    current_version_id: int | None = None
    tags: list[str] = Field(default_factory=list)

    @field_serializer("current_version_id")
    def serialize_current_version_id(self, v: int | None) -> str | None:
        return str(v) if v is not None else None


class AppQuery(BaseModel):
    """应用列表查询参数"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    current: int = 1
    size: int = 10
    keyword: str | None = None
    category: str | None = None
    status: str | None = "published"
    sort: str = "download"  # download | latest | rating


class VersionOut(AppBase):
    """应用版本输出"""

    id: int
    app_id: int
    version: str
    changelog: str | None = None
    manifest: dict[str, Any]
    file_size: int | None = None
    review_status: str
    created_at: datetime

    @field_serializer("id", "app_id")
    def serialize_id(self, v: int) -> str:
        return str(v)

    @field_serializer("created_at")
    def serialize_datetime(self, v: datetime) -> str:
        return v.strftime(settings.DATETIME_FORMAT)


class VersionUploadOut(VersionOut):
    """上传响应（VersionOut + reviewId）"""

    review_id: int = Field(alias="reviewId")

    @field_serializer("review_id")
    def serialize_review_id(self, v: int) -> str:
        return str(v)
