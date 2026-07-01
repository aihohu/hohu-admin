"""用户权限/菜单收集逻辑测试。

回归：get_user_info / get_user_routes 之前不过滤 role.status，
禁用角色仍然贡献 buttons 和菜单路由，导致管理员禁用角色后用户权限
不立即回收（直到用户重新登录）。

修复后 collect_user_buttons / collect_user_menus 必须只统计启用的角色。
"""

import itertools

from app.constants import (
    MENU_TYPE_DIRECTORY,
    MENU_TYPE_MENU,
    STATUS_ENABLED,
)
from app.modules.auth.permission_collect import (
    collect_user_buttons,
    collect_user_menus,
)
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User

_menu_id_seq = itertools.count(1000)


def _make_role(name: str, code: str, status: str = STATUS_ENABLED) -> Role:
    role = Role(role_name=name, role_code=code, status=status)
    return role


def _make_menu(
    *,
    name: str,
    menu_type: str = MENU_TYPE_MENU,
    permission: str | None = None,
    status: str = STATUS_ENABLED,
) -> Menu:
    return Menu(
        menu_id=next(_menu_id_seq),
        menu_name=name,
        menu_type=menu_type,
        permission=permission,
        status=status,
        order=0,
    )


def _attach_roles(user: User, roles: list[Role]) -> User:
    user.roles = roles
    return user


def _attach_menus(role: Role, menus: list[Menu]) -> Role:
    role.menus = menus
    return role


def test_buttons_only_from_enabled_roles():
    """禁用角色的按钮权限不应被收集。"""
    enabled_role = _attach_menus(
        _make_role("启用", "R_EN"),
        [_make_menu(name="m1", permission="sys:a:add")],
    )
    disabled_role = _attach_menus(
        _make_role("禁用", "R_DIS", status="2"),
        [_make_menu(name="m2", permission="sys:b:del")],
    )
    user = _attach_roles(
        User(user_name="u1", status=STATUS_ENABLED),
        [enabled_role, disabled_role],
    )

    buttons = collect_user_buttons(user)

    assert "sys:a:add" in buttons
    assert "sys:b:del" not in buttons


def test_buttons_dedup_across_roles():
    """多角色相同 permission 应去重。"""
    r1 = _attach_menus(
        _make_role("R1", "R_X1"),
        [_make_menu(name="m", permission="sys:dup")],
    )
    r2 = _attach_menus(
        _make_role("R2", "R_X2"),
        [_make_menu(name="m", permission="sys:dup")],
    )
    user = _attach_roles(User(user_name="u2", status=STATUS_ENABLED), [r1, r2])

    buttons = collect_user_buttons(user)

    assert buttons.count("sys:dup") == 1


def test_menus_only_from_enabled_roles():
    """禁用角色的菜单不应被收集。"""
    enabled_role = _attach_menus(
        _make_role("启用", "R_EN2"),
        [
            _make_menu(
                name="dir",
                menu_type=MENU_TYPE_DIRECTORY,
                permission=None,
            ),
            _make_menu(name="page", permission="sys:page:list"),
        ],
    )
    disabled_role = _attach_menus(
        _make_role("禁用", "R_DIS2", status="2"),
        [_make_menu(name="hidden", permission="sys:hidden:list")],
    )
    user = _attach_roles(
        User(user_name="u3", status=STATUS_ENABLED),
        [enabled_role, disabled_role],
    )

    menus = collect_user_menus(user)

    names = [m.menu_name for m in menus]
    assert "dir" in names
    assert "page" in names
    assert "hidden" not in names


def test_menus_skip_disabled_and_button_type():
    """禁用菜单和 F-type 按钮都不进入路由树。"""
    role = _attach_menus(
        _make_role("R", "R_MIX"),
        [
            _make_menu(name="enabled", permission="x:y:z"),
            _make_menu(name="disabled", permission="x:off", status="2"),
            _make_menu(
                name="btn",
                menu_type="F",
                permission="x:btn:op",
            ),
        ],
    )
    user = _attach_roles(User(user_name="u4", status=STATUS_ENABLED), [role])

    menus = collect_user_menus(user)

    names = [m.menu_name for m in menus]
    assert "enabled" in names
    assert "disabled" not in names
    assert "btn" not in names  # F-type 按钮不进入路由


def test_menus_dedup_across_roles():
    """多角色共享同一菜单时只返回一次。"""
    shared_menu = _make_menu(name="shared", permission="x:y:z")
    r1 = _attach_menus(_make_role("R1", "R_D1"), [shared_menu])
    r2 = _attach_menus(_make_role("R2", "R_D2"), [shared_menu])
    user = _attach_roles(User(user_name="u5", status=STATUS_ENABLED), [r1, r2])

    menus = collect_user_menus(user)

    assert len(menus) == 1
    assert menus[0].menu_name == "shared"
