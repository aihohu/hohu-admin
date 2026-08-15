"""用户权限/菜单收集。

抽取自 /getUserInfo 和 /getUserRoutes 端点，统一过滤 role.status，
避免禁用角色仍然贡献 buttons / 路由的不一致问题。
"""

from app.constants import (
    MENU_TYPE_DIRECTORY,
    MENU_TYPE_MENU,
    STATUS_ENABLED,
)
from app.modules.system.models.menu import Menu
from app.modules.system.models.user import User


def collect_user_permission_codes(user: User) -> set[str]:
    """收集启用角色显式关联的功能权限码。

    ``menu.status`` 当前只控制 M/C 路由可见性，不撤销角色已关联的功能
    权限；API、前端 buttons 与 AI Tool 必须共用这里，避免语义漂移。
    """
    perms: set[str] = set()
    for role in user.roles:
        if role.status != STATUS_ENABLED:
            continue
        for menu in role.menus:
            if menu.permission:
                perms.add(menu.permission)
    return perms


def collect_user_buttons(user: User) -> list[str]:
    """返回前端 buttons 需要的去重权限码列表。"""
    return list(collect_user_permission_codes(user))


def collect_user_menus(user: User) -> list[Menu]:
    """收集用户在启用角色下可访问的「启用 M/C 菜单」（按 menu_id 去重）。"""
    result: dict[int, Menu] = {}
    for role in user.roles:
        if role.status != STATUS_ENABLED:
            continue
        for menu in role.menus:
            if (
                menu.menu_type in (MENU_TYPE_DIRECTORY, MENU_TYPE_MENU)
                and menu.status == STATUS_ENABLED
            ):
                result[menu.menu_id] = menu
    return list(result.values())
