from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)
from pydantic.alias_generators import to_camel

from app.constants import STATUS_DISABLED, STATUS_ENABLED
from app.core.config import settings


class DataScopeDemoCreate(BaseModel):
    """演示数据创建请求。

    dept_id / create_by 不接受前端传值，service 层从 current_user 注入，
    防止前端伪造绕过数据权限语义。
    """

    title: str = Field(..., min_length=1, max_length=100, description="标题")
    content: str | None = Field(None, description="内容")
    status: str = Field(STATUS_ENABLED, description="状态：1-启用，2-禁用")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DataScopeDemoUpdate(BaseModel):
    """演示数据更新请求（仅允许改 title/content/status，不能改 dept_id）。"""

    title: str | None = Field(None, min_length=1, max_length=100, description="标题")
    content: str | None = Field(None, description="内容")
    status: str | None = Field(None, description="状态")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DataScopeDemoQuery(BaseModel):
    """演示数据查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    title: str | None = Field(None, description="标题（模糊查询）")
    status: str | None = Field(None, description="状态")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DataScopeDemoOut(BaseModel):
    """演示数据输出"""

    demo_id: int
    title: str
    content: str | None = None
    dept_id: int
    create_by: int
    status: str
    create_time: datetime | None = None
    update_time: datetime | None = None

    @field_serializer("demo_id")
    def serialize_id(self, v: int, _info):
        return str(v)

    @field_serializer("dept_id")
    def serialize_dept_id(self, v: int, _info):
        return str(v)

    @field_serializer("create_by")
    def serialize_create_by(self, v: int, _info):
        return str(v)

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str | None:
        if dt is None:
            return None
        return dt.strftime(settings.DATETIME_FORMAT)

    @field_serializer("update_time")
    def serialize_update_time(self, dt: datetime) -> str | None:
        if dt is None:
            return None
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        from_attributes=True, alias_generator=to_camel, populate_by_name=True
    )
