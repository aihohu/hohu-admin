from datetime import datetime
from re import match

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_serializer,
    field_validator,
)
from pydantic.alias_generators import to_camel

from app.core.config import settings
from app.utils.mask_util import MaskUtil


class UserDeptItem(BaseModel):
    """用户部门关联项"""

    dept_id: str = Field(..., description="部门ID")
    is_primary: bool = Field(False, description="是否主部门")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class UserBase(BaseModel):
    """用户基础字段"""

    user_name: str = Field(..., min_length=4, max_length=50, description="账号")
    nickname: str | None = Field(None, max_length=50, description="昵称")
    user_email: EmailStr = Field(..., description="邮箱")
    user_phone: str = Field(..., description="手机号")
    user_gender: str = Field(..., description="用户性别")
    status: str = Field(..., description="状态")
    roles: list[str] = []  # 创建时分配的角色 ID 列表
    dept_ids: list[UserDeptItem] = []  # 部门关联列表

    @field_validator("user_name")
    @classmethod
    def validate_user_name(cls, v: str) -> str:
        """验证用户名格式"""
        if not v.isalnum():
            raise ValueError("用户名只能包含字母和数字")
        return v

    @field_validator("user_phone")
    @classmethod
    def validate_user_phone(cls, v: str) -> str:
        """验证手机号格式"""
        if not match(r"^1[3-9]\d{9}$", v):
            raise ValueError("手机号格式不正确")
        return v

    @field_validator("user_gender")
    @classmethod
    def validate_user_gender(cls, v: str) -> str:
        """验证用户性别"""
        if v not in ["0", "1", "2"]:
            raise ValueError("用户性别必须是 0(未知)、1(男) 或 2(女)")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        """验证状态值"""
        if v not in ["1", "2"]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v

    @field_validator("roles")
    @classmethod
    def validate_roles(cls, v: list[str]) -> list[str]:
        """验证角色列表"""
        if v is None or len(v) == 0:
            raise ValueError("必须至少分配一个角色")
        return v

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class UserCreate(UserBase):
    """用户创建请求"""

    password: str = Field(..., min_length=6, max_length=20, description="明文密码")

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """验证密码强度"""
        if not any(char.isdigit() for char in v):
            raise ValueError("密码必须包含数字")
        if not any(char.isupper() for char in v):
            raise ValueError("密码必须包含大写字母")
        if not any(char.islower() for char in v):
            raise ValueError("密码必须包含小写字母")
        return v


class UserUpdate(UserBase):
    """用户更新请求"""

    password: str | None = Field(
        None, min_length=6, max_length=20, description="明文密码"
    )

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str | None) -> str | None:
        """验证密码强度（可选字段）"""
        if v is None:
            return v
        if not any(char.isdigit() for char in v):
            raise ValueError("密码必须包含数字")
        if not any(char.isupper() for char in v):
            raise ValueError("密码必须包含大写字母")
        if not any(char.islower() for char in v):
            raise ValueError("密码必须包含小写字母")
        return v


class UserLogin(BaseModel):
    """用户登录请求"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    user_name: str = Field(..., min_length=4, description="账号")
    password: str = Field(..., min_length=1, description="密码")


class UserQuery(BaseModel):
    """用户查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    user_name: str | None = Field(None, description="用户名（支持模糊查询）")
    nickname: str | None = Field(None, description="昵称（支持模糊查询）")
    user_phone: str | None = Field(None, description="手机号（支持模糊查询）")
    user_email: str | None = Field(None, description="邮箱（支持模糊查询）")
    user_gender: str | None = Field(None, description="用户性别")
    status: str | None = Field(None, description="状态")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class UserOut(BaseModel):
    """用户输出（单条）"""

    user_id: int
    user_name: str
    nickname: str
    status: str

    # 核心：返回给前端时转为字符串
    @field_serializer("user_id")
    def serialize_id(self, user_id: int, _info):
        return str(user_id)

    # 核心：允许 Pydantic 直接读取 SQLAlchemy 模型属性
    model_config = ConfigDict(from_attributes=True)


class UserItemOut(BaseModel):
    """用户列表显示对象"""

    user_id: int
    user_name: str
    nickname: str | None = None
    user_email: str | None = None
    user_phone: str | None = None
    user_gender: str | None = None
    status: str | None = None
    create_time: datetime
    # 可以在此扩展角色信息
    roles: list[str] = []
    dept_ids: list[str] = []
    dept_names: str = ""
    primary_dept: str | None = None

    model_config = ConfigDict(
        from_attributes=True, alias_generator=to_camel, populate_by_name=True
    )

    # 核心：处理 Snowflake ID 精度丢失问题
    @field_serializer("user_id")
    def serialize_id(self, user_id: int, _info):
        return str(user_id)

    @field_serializer("user_phone")
    def serialize_phone(self, v: str) -> str:
        return MaskUtil.phone(v)

    @field_serializer("user_email")
    def serialize_email(self, v: str) -> str:
        return MaskUtil.email(v)

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        """格式化创建时间"""
        return dt.strftime(settings.DATETIME_FORMAT)

    @field_validator("roles", mode="before")
    @classmethod
    def transform_roles(cls, v):
        """验证并转换角色列表"""
        # 如果传入的是 SQLAlchemy 的 Role 对象列表，则提取名称
        if v and len(v) > 0 and not isinstance(v[0], str):
            return [r.role_code for r in v]
        return v
