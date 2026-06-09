from datetime import datetime

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
from app.utils.validators import (
    empty_to_none,
    validate_gender,
    validate_password,
    validate_phone,
    validate_status,
    validate_user_name,
)


class UserDeptItem(BaseModel):
    """用户部门关联项"""

    dept_id: str = Field(..., description="部门ID")
    is_primary: bool = Field(False, description="是否主部门")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class UserBase(BaseModel):
    """用户基础字段"""

    user_name: str = Field(..., min_length=2, max_length=16, description="账号")
    nickname: str | None = Field(None, min_length=2, max_length=16, description="昵称")
    user_email: EmailStr | None = Field(None, description="邮箱")
    user_phone: str | None = Field(None, description="手机号")
    user_gender: str | None = Field(None, description="用户性别")
    status: str = Field(..., description="状态")
    roles: list[str] = []  # 创建时分配的角色 ID 列表
    dept_ids: list[UserDeptItem] = []  # 部门关联列表

    @field_validator("user_name")
    @classmethod
    def _validate_user_name(cls, v: str) -> str:
        return validate_user_name(v)

    @field_validator("user_phone", mode="before")
    @classmethod
    def _validate_user_phone(cls, v: str | None) -> str | None:
        return validate_phone(v)

    @field_validator("user_gender", mode="before")
    @classmethod
    def _validate_user_gender(cls, v: str | None) -> str | None:
        return validate_gender(v)

    @field_validator("user_email", mode="before")
    @classmethod
    def _validate_user_email(cls, v: str | None) -> str | None:
        return empty_to_none(v)

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        return validate_status(v)

    @field_validator("roles")
    @classmethod
    def validate_roles(cls, v: list[str]) -> list[str]:
        if v is None or len(v) == 0:
            raise ValueError("必须至少分配一个角色")
        return v

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class UserCreate(UserBase):
    """用户创建请求"""

    password: str = Field(..., min_length=6, max_length=20, description="明文密码")

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        return validate_password(v)


class UserUpdate(UserBase):
    """用户更新请求"""

    password: str | None = Field(None, description="明文密码")

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v: str | None) -> str | None:
        if not v:
            return None
        return validate_password(v)


class ResetPassword(BaseModel):
    """管理员重置用户密码请求"""

    new_password: str = Field(..., min_length=6, max_length=20, description="新密码")

    @field_validator("new_password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        return validate_password(v)

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ChangePassword(BaseModel):
    """用户修改自己的密码"""

    old_password: str = Field(..., min_length=1, description="当前密码")
    new_password: str = Field(..., min_length=6, max_length=20, description="新密码")

    @field_validator("new_password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        return validate_password(v)

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class UpdateProfile(BaseModel):
    """用户更新个人信息"""

    nickname: str | None = Field(None, min_length=2, max_length=16, description="昵称")
    user_avatar: str | None = Field(None, description="头像URL")
    user_gender: str | None = Field(None, description="性别")
    user_phone: str | None = Field(None, description="手机号")
    user_email: EmailStr | None = Field(None, description="邮箱")

    @field_validator("user_gender", mode="before")
    @classmethod
    def _validate_gender(cls, v: str | None) -> str | None:
        return validate_gender(v)

    @field_validator("user_phone", mode="before")
    @classmethod
    def _validate_phone(cls, v: str | None) -> str | None:
        return validate_phone(v)

    @field_validator("user_email", mode="before")
    @classmethod
    def _validate_email(cls, v: str | None) -> str | None:
        return empty_to_none(v)

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ProfileOut(BaseModel):
    """个人信息输出"""

    user_id: int
    user_name: str
    nickname: str = ""
    user_gender: str = "0"
    user_phone: str = ""
    user_email: str = ""
    user_avatar: str = ""
    status: str
    roles: list[str] = []
    create_time: str = ""

    model_config = ConfigDict(
        from_attributes=True, alias_generator=to_camel, populate_by_name=True
    )

    @field_serializer("user_id")
    def serialize_id(self, user_id: int, _info):
        return str(user_id)


class UserLogin(BaseModel):
    """用户登录请求"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    user_name: str = Field(..., min_length=2, max_length=16, description="账号")
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

    @field_serializer("user_id")
    def serialize_id(self, user_id: int, _info):
        return str(user_id)

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
    roles: list[str] = []
    dept_ids: list[str] = []
    dept_names: str = ""
    primary_dept: str | None = None

    model_config = ConfigDict(
        from_attributes=True, alias_generator=to_camel, populate_by_name=True
    )

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
        return dt.strftime(settings.DATETIME_FORMAT)

    @field_validator("roles", mode="before")
    @classmethod
    def transform_roles(cls, v):
        if v and len(v) > 0 and not isinstance(v[0], str):
            return [r.role_code for r in v]
        return v
