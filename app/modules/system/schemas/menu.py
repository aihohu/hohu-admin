from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel


class ButtonCreate(BaseModel):
    desc: str
    code: str


class MenuBase(BaseModel):
    parent_id: int | None = None
    menu_name: str
    menu_type: str  # M目录, C菜单, F按钮
    icon: str | None = None
    icon_type: str | None = None
    path: str | None = None
    component: str | None = None
    route_name: str | None = None
    route_path: str | None = None
    i18n_key: str | None = Field(None, alias="i18nKey")
    order: int = 0
    status: str = "1"

    active_menu: str | None = None
    fixed_index_in_tab: int | None = None
    hide_in_menu: bool | None = None
    href: str | None = None
    keep_alive: bool | None = None
    constant: bool | None = None
    layout: str | None = None
    multi_tab: bool | None = None
    page: str | None = None
    path_param: str | None = None

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class MenuCreate(MenuBase):
    # buttons: list
    query: list


class MenuUpdate(MenuBase):
    buttons: list[ButtonCreate]
    query: list

    # 全部设为可选，支持局部更新
    # 如果字段非常多，可以考虑使用 Pydantic 的 create_model 或继承
    model_config = ConfigDict(from_attributes=True)


class MenuQuery(BaseModel):
    current: int = 1
    size: int = 10

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class MenuOut(MenuBase):
    menu_id: int
    create_time: datetime

    @field_serializer("menu_id", "parent_id")
    def serialize_id(self, v: int, _info):
        return str(v) if v is not None else None

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class MenuSimpleOut(MenuBase):
    menu_id: int
    create_time: datetime

    @field_serializer("menu_id", "parent_id")
    def serialize_id(self, v: int, _info):
        return str(v) if v is not None else None

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class MenuTreeOut(MenuOut):
    children: list["MenuTreeOut"] = []
    buttons: list[ButtonCreate] = []


class MenuTreeOptionOut(BaseModel):
    id: int
    label: str
    p_id: str
    children: list["MenuTreeOptionOut"] = []

    @field_serializer("id")
    def serialize_id(self, v: int, _info):
        return str(v) if v is not None else None

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
