"""Immutable hosted menu manifest used by tenant bootstrap.

The platform control plane must never copy mutable rows from Default Tenant:
that would let a tenant-local menu editor influence a future tenant's routes.
"""

from dataclasses import dataclass, fields
from typing import Any

from app.constants import (
    MENU_TYPE_BUTTON,
    MENU_TYPE_DIRECTORY,
    MENU_TYPE_MENU,
    STATUS_ENABLED,
)
from app.core.id_generator import next_id
from app.modules.system.models.menu import Menu


@dataclass(frozen=True, slots=True)
class HostedMenuBlueprint:
    key: str
    parent_key: str | None
    menu_name: str
    menu_type: str
    route_name: str | None = None
    route_path: str | None = None
    component: str | None = None
    page: str | None = None
    layout: str | None = None
    i18n_key: str | None = None
    icon: str | None = None
    icon_type: str | None = None
    order: int = 0
    hide_in_menu: bool | None = None
    keep_alive: bool | None = None
    constant: bool | None = None
    multi_tab: bool | None = None
    permission: str | None = None


def _directory(
    key: str,
    name: str,
    route_path: str,
    *,
    icon: str,
    order: int,
    parent_key: str | None = None,
) -> HostedMenuBlueprint:
    return HostedMenuBlueprint(
        key=key,
        parent_key=parent_key,
        menu_name=name,
        menu_type=MENU_TYPE_DIRECTORY,
        route_name=key,
        route_path=route_path,
        component="layout.base",
        layout="base",
        i18n_key=f"route.{key}",
        icon=icon,
        icon_type="1",
        order=order,
        hide_in_menu=False,
        keep_alive=False,
        constant=False,
        multi_tab=False,
    )


def _page(
    key: str,
    parent_key: str | None,
    name: str,
    route_path: str,
    *,
    icon: str,
    order: int,
    hide_in_menu: bool = False,
    permission: str | None = None,
    home: bool = False,
) -> HostedMenuBlueprint:
    return HostedMenuBlueprint(
        key=key,
        parent_key=parent_key,
        menu_name=name,
        menu_type=MENU_TYPE_MENU,
        route_name=key,
        route_path=route_path,
        component=(f"layout.base$view.{key}" if home else f"view.{key}"),
        page=key,
        layout="base" if home else None,
        i18n_key=f"route.{key}",
        icon=icon,
        icon_type="1",
        order=order,
        hide_in_menu=hide_in_menu,
        keep_alive=False,
        constant=False,
        multi_tab=False,
        permission=permission,
    )


def _permission(parent_key: str, permission: str, name: str) -> HostedMenuBlueprint:
    return HostedMenuBlueprint(
        key=f"permission:{permission}",
        parent_key=parent_key,
        menu_name=name,
        menu_type=MENU_TYPE_BUTTON,
        permission=permission,
    )


HOSTED_MENU_BLUEPRINTS = (
    _page(
        "home",
        None,
        "首页",
        "/home",
        icon="carbon:home",
        order=0,
        home=True,
    ),
    _directory("ai", "AI 助手", "/ai", icon="carbon:chat-bot", order=1),
    _directory("auth", "权限管理", "/auth", icon="carbon:security", order=98),
    _directory(
        "system",
        "系统管理",
        "/system",
        icon="carbon:cloud-service-management",
        order=99,
    ),
    _directory("task", "任务中心", "/task", icon="carbon:task", order=100),
    _page(
        "ai_chat",
        "ai",
        "AI 对话",
        "/ai/chat",
        icon="carbon:chat",
        order=1,
    ),
    _page(
        "ai_trace",
        "ai",
        "AI Trace 查看",
        "/ai/trace",
        icon="carbon:flow-logs-vpc",
        order=4,
        permission="ai:trace:view",
    ),
    _page(
        "system_dept",
        "auth",
        "部门管理",
        "/system/dept",
        icon="carbon:user-multiple",
        order=1,
    ),
    _page(
        "system_user",
        "auth",
        "用户管理",
        "/system/user",
        icon="carbon:user",
        order=2,
    ),
    _page(
        "system_role",
        "auth",
        "角色管理",
        "/system/role",
        icon="carbon:user-role",
        order=3,
    ),
    _page(
        "system_menu",
        "auth",
        "菜单管理",
        "/system/menu",
        icon="carbon:menu",
        order=4,
    ),
    _page(
        "system_dict",
        "system",
        "字典管理",
        "/system/dict",
        icon="fluent-mdl2:dictionary",
        order=1,
    ),
    _page(
        "system_dict_data",
        "system",
        "字典数据",
        "/system/dict/data",
        icon="fluent-mdl2:dictionary",
        order=2,
        hide_in_menu=True,
    ),
    _page(
        "system_file",
        "system",
        "文件管理",
        "/system/file",
        icon="carbon:cloud-upload",
        order=3,
    ),
    _directory(
        "system_monitor",
        "监控管理",
        "/system/monitor",
        icon="carbon:activity",
        order=10,
        parent_key="system",
    ),
    _page(
        "system_job",
        "task",
        "定时任务",
        "/system/job",
        icon="carbon:timer",
        order=1,
    ),
    _page(
        "system_job-log",
        "task",
        "任务日志",
        "/system/job-log",
        icon="carbon:document-tasks",
        order=2,
    ),
    _page(
        "system_operation-log",
        "system_monitor",
        "操作日志",
        "/system/operation-log",
        icon="carbon:document",
        order=1,
    ),
    _page(
        "system_login-log",
        "system_monitor",
        "登录日志",
        "/system/login-log",
        icon="carbon:login",
        order=2,
    ),
    _permission("ai_chat", "ai:chat:use", "使用 AI 对话"),
    _permission("ai_chat", "ai:file:parse", "解析聊天文件"),
    _permission("system_dept", "system:dept:list", "查询"),
    _permission("system_dept", "system:dept:add", "新增"),
    _permission("system_dept", "system:dept:edit", "修改"),
    _permission("system_dept", "system:dept:move", "移动"),
    _permission("system_dept", "system:dept:delete", "删除"),
    _permission("system_dept", "system:dept:batch-delete", "批量删除"),
    _permission("system_user", "system:user:list", "查询"),
    _permission("system_user", "system:user:add", "新增"),
    _permission("system_user", "system:user:edit", "修改"),
    _permission("system_user", "system:user:delete", "删除"),
    _permission("system_user", "system:user:reset-password", "重置密码"),
    _permission("system_user", "system:user:import", "导入"),
    _permission("system_user", "system:user:export", "导出"),
    _permission("system_user", "system:user:role-auth", "角色授权"),
    _permission("system_role", "system:role:list", "查询"),
    _permission("system_role", "system:role:add", "新增"),
    _permission("system_role", "system:role:edit", "修改"),
    _permission("system_role", "system:role:menu-auth", "菜单授权"),
    _permission("system_role", "system:role:ai-agent-auth", "AI Agent 授权"),
    _permission("system_role", "system:role:delete", "删除"),
    _permission("system_role", "system:role:batch-delete", "批量删除"),
    _permission("system_job", "system:job:edit", "修改"),
    _permission("system_operation-log", "monitor:operation-log:list", "查询"),
    _permission("system_login-log", "monitor:login-log:list", "查询"),
)

HOSTED_ROUTE_NAMES = frozenset(
    blueprint.route_name
    for blueprint in HOSTED_MENU_BLUEPRINTS
    if blueprint.route_name is not None
)
HOSTED_PERMISSION_CODES = frozenset(
    blueprint.permission
    for blueprint in HOSTED_MENU_BLUEPRINTS
    if blueprint.permission is not None
)


def build_hosted_tenant_menus(tenant_id: int) -> list[Menu]:
    ids = {blueprint.key: next_id() for blueprint in HOSTED_MENU_BLUEPRINTS}
    if len(ids) != len(HOSTED_MENU_BLUEPRINTS):
        raise RuntimeError("hosted menu manifest contains duplicate logical keys")
    menus: list[Menu] = []
    for blueprint in HOSTED_MENU_BLUEPRINTS:
        values: dict[str, Any] = {
            item.name: getattr(blueprint, item.name)
            for item in fields(HostedMenuBlueprint)
            if item.name not in {"key", "parent_key"}
        }
        menus.append(
            Menu(
                tenant_id=tenant_id,
                menu_id=ids[blueprint.key],
                parent_id=(
                    ids[blueprint.parent_key]
                    if blueprint.parent_key is not None
                    else None
                ),
                status=STATUS_ENABLED,
                **values,
            )
        )
    return menus
