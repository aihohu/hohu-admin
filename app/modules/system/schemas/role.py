from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from pydantic.alias_generators import to_camel

from app.constants import (
    STATUS_DISABLED,
    STATUS_ENABLED,
)


class RoleBase(BaseModel):
    """角色基础字段"""

    role_name: str = Field(..., min_length=2, max_length=50, description="角色名称")
    role_code: str = Field(
        ...,
        min_length=2,
        max_length=50,
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$",
        description="角色编码",
    )
    role_desc: str | None = Field(None, max_length=200, description="角色描述")
    status: str = Field(
        ...,
        description="状态：1-启用，2-禁用",
    )

    @field_validator("role_name")
    @classmethod
    def validate_role_name(cls, v: str) -> str:
        """验证角色名称"""
        if not v or v.strip() == "":
            raise ValueError("角色名称不能为空")
        return v.strip()

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        """验证状态值"""
        if v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v


class RoleCreate(RoleBase):
    """角色创建请求"""

    pass


class RoleUpdate(BaseModel):
    """角色更新请求"""

    role_name: str | None = Field(
        None, min_length=2, max_length=50, description="角色名称"
    )
    role_code: str | None = Field(
        None,
        min_length=2,
        max_length=50,
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$",
        description="角色编码",
    )
    role_desc: str | None = Field(None, max_length=200, description="角色描述")
    status: str | None = Field(None, description="状态：1-启用，2-禁用")

    @field_validator("role_name")
    @classmethod
    def validate_role_name(cls, v: str | None) -> str | None:
        """验证角色名称（可选字段）"""
        if v is not None and (not v or v.strip() == ""):
            raise ValueError("角色名称不能为空")
        return v.strip() if v is not None else None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        """验证状态值（可选字段）"""
        if v is not None and v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v


class RoleOut(RoleBase):
    """角色输出"""

    role_id: int
    create_time: datetime

    @field_serializer("role_id")
    def serialize_id(self, role_id: int, _info):
        return str(role_id)

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class RoleQuery(BaseModel):
    """角色查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    role_name: str | None = Field(None, description="角色名称（支持模糊查询）")
    role_code: str | None = Field(None, description="角色编码（支持模糊查询）")
    status: str | None = Field(None, description="状态")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        """验证状态值（可选字段）"""
        if v is not None and v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RoleSimpleOut(BaseModel):
    """角色简单输出（用于下拉选择）"""

    role_id: int
    role_name: str
    role_code: str
    status: str

    @field_serializer("role_id")
    def serialize_id(self, role_id: int, _info):
        return str(role_id)

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
