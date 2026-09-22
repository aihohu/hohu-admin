"""data_scope 权限工具测试。

is_super_admin 必须与 app.core.auth.is_super_admin 行为一致：
- user_name == "admin" 本身不提供权限
- 含启用 R_SUPER 角色的用户视为所在范围的管理员
- 否则不是超管

2026-09-18：权限与数据范围统一按启用角色授权，移除用户名特权。
"""

from app.constants import ADMIN_USERNAME, STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.utils.data_scope import is_super_admin


def _make_user(name: str, roles: list[Role]) -> User:
    user = User(user_name=name, status=STATUS_ENABLED)
    user.roles = roles
    return user


def test_admin_username_is_not_super_admin_without_role():
    """用户名不授予权限，必须拥有启用的管理员角色。"""
    user = _make_user(ADMIN_USERNAME, roles=[])
    assert is_super_admin(user) is False


def test_super_role_is_super_admin():
    """挂了 R_SUPER 角色的普通用户名也是超管。"""
    role = Role(role_code=SUPER_ADMIN_ROLE_CODE, status=STATUS_ENABLED)
    user = _make_user("alice", roles=[role])
    assert is_super_admin(user) is True


def test_normal_user_not_super_admin():
    """普通用户名 + 普通角色不是超管。"""
    role = Role(role_code="R_USER", status=STATUS_ENABLED)
    user = _make_user("bob", roles=[role])
    assert is_super_admin(user) is False


def test_disabled_super_role_not_super_admin():
    """R_SUPER 角色被禁用时（status != 1）不应视为超管。

    与 app.core.auth.require_permissions 行为对齐：那里只看 status == 1。
    """
    role = Role(role_code=SUPER_ADMIN_ROLE_CODE, status="2")
    user = _make_user("charlie", roles=[role])
    assert is_super_admin(user) is False
