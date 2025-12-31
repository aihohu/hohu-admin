from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class LoginCredentials(BaseModel):
    """通用登录请求体 (适配 JSON)"""

    login_type: str = Field(
        "password", description="登录类型: password, sms, wechat, google"
    )
    user_name: str | None = None
    password: str | None = None
    mobile: str | None = None
    code: str | None = None  # 验证码
    token: str | None = None  # 第三方(WeChat/Google)授权码

    model_config = ConfigDict(
        alias_generator=to_camel,  # 自动将所有字段名转为驼峰作为别名
        populate_by_name=True,  # 允许通过字段名或别名进行初始化
    )


class RouteMeta(BaseModel):
    title: str
    i18n_key: str | None = Field(None, alias="i18nKey")
    keep_alive: bool | None = None
    constant: bool | None = None
    icon: str | None = None
    order: int = 0
    href: str | None = None
    hide_in_menu: bool | None = None
    active_menu: str | None = None
    multi_tab: bool | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class UserRoute(BaseModel):
    name: str
    path: str
    component: str
    # props: bool | None = None
    meta: RouteMeta
    children: list["UserRoute"] | None = None

    class Config:
        from_attributes = True
