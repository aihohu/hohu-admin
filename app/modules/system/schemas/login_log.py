from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from pydantic.alias_generators import to_camel

from app.core.config import settings


class LoginLogQuery(BaseModel):
    """登录日志查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    username: str | None = Field(None, description="登录用户名")
    status: str | None = Field(None, description="登录状态")
    ip: str | None = Field(None, description="登录IP")
    start_time: datetime | None = Field(None, description="登录时间（起）")
    end_time: datetime | None = Field(None, description="登录时间（止）")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    @field_validator("start_time", "end_time", mode="after")
    @classmethod
    def _strip_tzinfo(cls, v: datetime | None) -> datetime | None:
        # DB 列为 TIMESTAMP WITHOUT TIME ZONE，需归一化为 naive UTC
        if v is not None and v.tzinfo is not None:
            return v.astimezone(UTC).replace(tzinfo=None)
        return v


class LoginLogOut(BaseModel):
    """登录日志输出"""

    login_log_id: int
    user_id: int | None
    username: str
    ip: str | None
    user_agent: str | None
    status: str
    message: str | None
    login_time: datetime

    @field_serializer("login_log_id")
    def serialize_log_id(self, v: int, _info):
        return str(v)

    @field_serializer("user_id")
    def serialize_user_id(self, v: int | None, _info):
        return str(v) if v is not None else None

    @field_serializer("login_time")
    def serialize_login_time(self, dt: datetime) -> str:
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )
