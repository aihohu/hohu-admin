"""部门相关的数据验证模式"""

import re
from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from app.constants import STATUS_DISABLED, STATUS_ENABLED
from app.core.config import settings


class DeptCreate(BaseModel):
    """部门创建请求"""

    parent_id: int | None = Field(None, description="父部门ID，NULL 表示顶级")
    dept_name: str = Field(..., min_length=1, max_length=100, description="部门名称")
    order_num: int = Field(0, ge=0, description="显示顺序")
    leader: str | None = Field(None, max_length=50, description="负责人")
    phone: str | None = Field(None, max_length=20, description="联系电话")
    email: str | None = Field(None, description="邮箱")
    status: str = Field(..., description="状态：1-启用，2-禁用")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DeptUpdate(BaseModel):
    """Department non-structural update request."""

    dept_name: str | None = Field(
        None, min_length=1, max_length=100, description="部门名称"
    )
    order_num: int | None = Field(None, ge=0, description="显示顺序")
    leader: str | None = Field(None, max_length=50, description="负责人")
    phone: str | None = Field(None, max_length=20, description="联系电话")
    email: str | None = Field(None, description="邮箱")
    status: str | None = Field(None, description="状态：1-启用，2-禁用")

    @field_validator("dept_name", "order_num")
    @classmethod
    def reject_null_required_fields(cls, v: object) -> object:
        """Reject explicit nulls for fields backed by non-null columns."""
        if v is None:
            raise ValueError("部门名称和显示顺序不能为 NULL")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is None:
            raise ValueError("状态不能为 NULL")
        if v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "DeptUpdate":
        """Reject empty updates instead of accepting a no-op write."""
        if not self.model_fields_set:
            raise ValueError("至少需要提供一个更新字段")
        return self

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class DeptMove(BaseModel):
    """Canonical department hierarchy move request."""

    new_parent_id: int | None = Field(..., description="新父部门ID，NULL 表示顶级")

    @field_validator("new_parent_id", mode="before")
    @classmethod
    def validate_new_parent_id(cls, value: object) -> object:
        """Accept only a canonical positive Snowflake string or explicit null."""
        if value is None:
            return None
        if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
            raise ValueError("newParentId must be a canonical Snowflake ID string")
        return int(value)

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class DeptQuery(BaseModel):
    """部门查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    dept_name: str | None = Field(None, description="部门名称（模糊查询）")
    status: str | None = Field(None, description="状态")
    leader: str | None = Field(None, description="负责人（模糊查询）")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DeptOut(BaseModel):
    """部门输出"""

    dept_id: int
    parent_id: int | None = None
    ancestors: str | None = None
    dept_name: str
    order_num: int
    leader: str | None = None
    phone: str | None = None
    email: str | None = None
    status: str
    create_by: str | None = None
    create_time: datetime | None = None
    update_by: str | None = None
    update_time: datetime | None = None

    @field_serializer("dept_id")
    def serialize_id(self, v: int, _info):
        return str(v)

    @field_serializer("parent_id")
    def serialize_parent_id(self, v: int, _info):
        return str(v) if v is not None else None

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        if dt is None:
            return None
        return dt.strftime(settings.DATETIME_FORMAT)

    @field_serializer("update_time")
    def serialize_update_time(self, dt: datetime) -> str:
        if dt is None:
            return None
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        from_attributes=True, alias_generator=to_camel, populate_by_name=True
    )


class DeptTreeOut(DeptOut):
    """部门树形输出"""

    children: list["DeptTreeOut"] = []

    @field_serializer("dept_id")
    def serialize_id(self, v: int, _info):
        return str(v)

    @field_serializer("parent_id")
    def serialize_parent_id(self, v: int, _info):
        return str(v) if v is not None else None

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        if dt is None:
            return None
        return dt.strftime(settings.DATETIME_FORMAT)

    @field_serializer("update_time")
    def serialize_update_time(self, dt: datetime) -> str:
        if dt is None:
            return None
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        from_attributes=True, alias_generator=to_camel, populate_by_name=True
    )


class DeptTreeOptionOut(BaseModel):
    """部门树形选项输出（用于下拉选择）"""

    id: int
    label: str
    p_id: str
    children: list["DeptTreeOptionOut"] = []

    @field_serializer("id")
    def serialize_id(self, v: int, _info):
        return str(v)

    model_config = ConfigDict(
        from_attributes=True, alias_generator=to_camel, populate_by_name=True
    )


class DeptUserItem(BaseModel):
    """Minimal user candidate for complete department membership editing."""

    user_id: int
    user_name: str
    nickname: str | None = None
    status: str
    is_member: bool = False
    is_primary: bool = False

    @field_serializer("user_id")
    def serialize_id(self, v: int, _info):
        return str(v)

    model_config = ConfigDict(
        from_attributes=True, alias_generator=to_camel, populate_by_name=True
    )


class DeptUsersOut(BaseModel):
    """Stable page of user-scoped department membership candidates."""

    current: int
    size: int
    total: int
    records: list[DeptUserItem] = Field(default_factory=list)

    model_config = ConfigDict(
        from_attributes=True, alias_generator=to_camel, populate_by_name=True
    )


class DeptUsersUpdate(BaseModel):
    """Complete final member ID set for one department."""

    user_ids: list[str] = Field(..., description="最终的部门成员用户ID列表")

    @field_validator("user_ids", mode="before")
    @classmethod
    def validate_user_ids(cls, value: object) -> object:
        if not isinstance(value, list) or any(
            not isinstance(item, str) or re.fullmatch(r"[1-9][0-9]*", item) is None
            for item in value
        ):
            raise ValueError("userIds must contain canonical Snowflake ID strings")
        if len(set(value)) != len(value):
            raise ValueError("userIds must not contain duplicates")
        return value

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )
