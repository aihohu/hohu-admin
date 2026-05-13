"""菜单相关的数据验证模式"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from pydantic.alias_generators import to_camel

from app.constants import (
    MENU_TYPE_BUTTON,
    MENU_TYPE_DIRECTORY,
    MENU_TYPE_MENU,
    STATUS_DISABLED,
    STATUS_ENABLED,
)
from app.core.config import settings


class ButtonCreate(BaseModel):
    """按钮创建请求"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    desc: str = Field(..., min_length=1, max_length=50, description="按钮描述")
    code: str = Field(..., min_length=1, max_length=100, description="权限代码")

    @field_validator("desc")
    @classmethod
    def validate_desc(cls, v: str) -> str:
        """验证按钮描述"""
        if not v or v.strip() == "":
            raise ValueError("按钮描述不能为空")
        return v.strip()

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        """验证权限代码"""
        if not v or v.strip() == "":
            raise ValueError("权限代码不能为空")
        return v.strip()


class MenuBase(BaseModel):
    """菜单基础字段"""

    parent_id: int | None = Field(None, description="父菜单ID")
    menu_name: str = Field(..., min_length=1, max_length=50, description="菜单名称")
    menu_type: str = Field(
        ...,
        description="菜单类型：M-目录，C-菜单，F-按钮",
    )
    icon: str | None = Field(None, max_length=100, description="图标")
    icon_type: str | None = Field(None, max_length=50, description="图标类型")
    path: str | None = Field(None, max_length=200, description="路径")
    component: str | None = Field(None, max_length=100, description="组件")
    route_name: str | None = Field(
        None,
        min_length=1,
        max_length=100,
        description="路由名称",
    )
    route_path: str | None = Field(None, max_length=200, description="路由路径")
    i18n_key: str | None = Field(
        None, max_length=100, alias="i18nKey", description="国际化key"
    )
    order: int = Field(0, ge=0, description="排序")
    status: str = Field(..., description="状态：1-启用，2-禁用")
    active_menu: str | None = Field(None, max_length=100, description="激活菜单")
    fixed_index_in_tab: int | None = Field(None, ge=0, description="Tab中固定索引")
    hide_in_menu: bool | None = Field(None, description="是否在菜单中隐藏")
    href: str | None = Field(None, max_length=200, description="外链")
    keep_alive: bool | None = Field(None, description="是否保持缓存")
    constant: bool | None = Field(None, description="是否常量菜单")
    layout: str | None = Field(None, max_length=50, description="布局")
    multi_tab: bool | None = Field(None, description="是否多标签")
    page: str | None = Field(None, max_length=100, description="页面")
    path_param: str | None = Field(None, max_length=100, description="路径参数")

    @field_validator("menu_name")
    @classmethod
    def validate_menu_name(cls, v: str) -> str:
        """验证菜单名称"""
        if not v or v.strip() == "":
            raise ValueError("菜单名称不能为空")
        return v.strip()

    @field_validator("menu_type")
    @classmethod
    def validate_menu_type(cls, v: str) -> str:
        """验证菜单类型"""
        if v not in [MENU_TYPE_DIRECTORY, MENU_TYPE_MENU, MENU_TYPE_BUTTON]:
            raise ValueError("菜单类型必须是 M(目录)、C(菜单) 或 F(按钮)")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        """验证状态值"""
        if v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v

    @field_validator("order")
    @classmethod
    def validate_order(cls, v: int) -> int:
        """验证排序值"""
        if v < 0:
            raise ValueError("排序值必须大于或等于0")
        return v

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class MenuCreate(MenuBase):
    """菜单创建请求"""

    buttons: list[ButtonCreate] = Field(default_factory=list, description="按钮列表")
    query: list = Field(default_factory=list, description="查询参数")


class MenuUpdate(MenuBase):
    """菜单更新请求"""

    buttons: list[ButtonCreate] | None = Field(None, description="按钮列表")
    query: list | None = Field(None, description="查询参数")


class MenuQuery(BaseModel):
    """菜单查询参数"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")


class MenuOut(MenuBase):
    """菜单输出"""

    menu_id: int
    create_time: datetime

    @field_serializer("menu_id")
    def serialize_id(self, v: int, _info):
        return str(v)

    @field_serializer("parent_id")
    def serialize_parent_id(self, v: int, _info):
        return str(v) if v is not None else None

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        """格式化创建时间"""
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class MenuSimpleOut(BaseModel):
    """菜单简单输出"""

    menu_id: int
    menu_name: str
    create_time: datetime

    @field_serializer("menu_id")
    def serialize_id(self, v: int, _info):
        return str(v)

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        """格式化创建时间"""
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class MenuTreeOut(MenuOut):
    """菜单树形输出"""

    children: list["MenuTreeOut"] = []
    buttons: list[ButtonCreate] = []

    @field_serializer("menu_id")
    def serialize_id(self, v: int, _info):
        return str(v)

    @field_serializer("parent_id")
    def serialize_parent_id(self, v: int, _info):
        return str(v) if v is not None else None

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        """格式化创建时间"""
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class MenuTreeOptionOut(BaseModel):
    """菜单树形选项输出（用于下拉选择）"""

    id: int
    label: str
    p_id: str
    children: list["MenuTreeOptionOut"] = []

    @field_serializer("id")
    def serialize_id(self, v: int, _info):
        return str(v)

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
