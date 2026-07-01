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


def collect_user_buttons(user: User) -> list[str]:
    """收集用户在启用角色下拥有的全部按钮权限码（已去重）。"""
    perms: set[str] = set()
    for role in user.roles:
        if role.status != STATUS_ENABLED:
            continue
        for menu in role.menus:
            if menu.permission:
                perms.add(menu.permission)
    return list(perms)


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
