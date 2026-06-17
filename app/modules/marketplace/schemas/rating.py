from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from pydantic.alias_generators import to_camel

from app.core.config import settings


class RatingBase(BaseModel):
    """评分 Schema 公共配置"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class RatingCreate(RatingBase):
    """创建评分请求（app_id 为 Snowflake 字符串形式）"""

    app_id: str
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = Field(None, max_length=500)

    @field_validator("app_id")
    @classmethod
    def validate_app_id(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("app_id 必须是数字字符串")
        return v


class RatingOut(RatingBase):
    """评分输出"""

    id: int
    app_id: int
    user_id: int
    rating: int
    comment: str | None = None
    created_at: datetime

    @field_serializer("id", "app_id", "user_id")
    def serialize_id(self, v: int) -> str:
        return str(v)

    @field_serializer("created_at")
    def serialize_datetime(self, v: datetime) -> str:
        return v.strftime(settings.DATETIME_FORMAT)
