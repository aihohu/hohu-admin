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

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
    STATUS_DISABLED,
    STATUS_ENABLED,
)
from app.core.config import settings

VALID_DATA_SCOPES = [
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
]


class RoleBase(BaseModel):
    """角色基础字段"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    role_name: str = Field(..., min_length=2, max_length=50, description="角色名称")
    role_code: str = Field(
        ...,
        min_length=2,
        max_length=50,
        pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$",
        description="角色编码",
    )
    role_desc: str | None = Field(None, max_length=200, description="角色描述")
    data_scope: str = Field(
        DATA_SCOPE_ALL,
        description="数据权限范围：1-全部，2-自定义，3-本部门，4-本部门及以下，5-仅本人",
    )
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

    @field_validator("data_scope")
    @classmethod
    def validate_data_scope(cls, v: str) -> str:
        if v not in VALID_DATA_SCOPES:
            raise ValueError("数据权限范围必须是 1~5")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        """验证状态值"""
        if v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v


class RoleCreate(RoleBase):
    """角色创建请求"""

    dept_ids: list[int] | None = Field(
        None, description="自定义数据权限时的部门ID列表（data_scope=2时生效）"
    )


class RoleUpdate(BaseModel):
    """角色更新请求"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

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
    data_scope: str | None = Field(
        None,
        description="数据权限范围：1-全部，2-自定义，3-本部门，4-本部门及以下，5-仅本人",
    )
    dept_ids: list[int] | None = Field(
        None, description="自定义数据权限时的部门ID列表（data_scope=2时生效）"
    )
    status: str | None = Field(None, description="状态：1-启用，2-禁用")

    @field_validator("role_name")
    @classmethod
    def validate_role_name(cls, v: str | None) -> str | None:
        """验证角色名称（可选字段）"""
        if v is not None and (not v or v.strip() == ""):
            raise ValueError("角色名称不能为空")
        return v.strip() if v is not None else None

    @field_validator("data_scope")
    @classmethod
    def validate_data_scope(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_DATA_SCOPES:
            raise ValueError("数据权限范围必须是 1~5")
        return v

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
    dept_ids: list[int] = Field(
        default_factory=list, description="自定义数据权限关联的部门ID列表"
    )

    @model_validator(mode="before")
    @classmethod
    def extract_dept_ids(cls, data: any) -> any:
        """从 Role ORM 对象的 depts 关系提取 dept_ids。

        返回 dict（拷贝 ORM 字段 + 注入 dept_ids），不改原 ORM 实例的
        __dict__，避免污染 SQLAlchemy 实例状态。
        """
        if isinstance(data, dict):
            return data
        if hasattr(data, "depts"):
            # vars() 取 __dict__，过滤 _sa_instance_state 等 SQLAlchemy 内部字段
            attrs = {k: v for k, v in vars(data).items() if not k.startswith("_")}
            attrs["dept_ids"] = [d.dept_id for d in data.depts]
            return attrs
        return data

    @field_serializer("role_id")
    def serialize_id(self, role_id: int, _info):
        return str(role_id)

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        """格式化创建时间"""
        return dt.strftime(settings.DATETIME_FORMAT)

    @field_serializer("dept_ids")
    def serialize_dept_ids(self, dept_ids: list[int]) -> list[str]:
        return [str(did) for did in dept_ids]

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )


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

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )
