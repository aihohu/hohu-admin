"""权限检查装饰器"""

from fastapi import Depends

from app.constants import ADMIN_USERNAME, STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.exceptions import AuthorizationException
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User


def is_super_admin(user: User) -> bool:
    """判断用户是否为超级管理员"""
    return user.user_name == ADMIN_USERNAME or SUPER_ADMIN_ROLE_CODE in [
        r.role_code for r in user.roles
    ]


def require_permissions(perm_code: str = None, super_admin_only: bool = False):
    """
    权限校验装饰器

    用法示例:
        @router.get("/list", dependencies=[Depends(require_permissions("sys:user:list"))])
        @router.get("/admin-only", dependencies=[Depends(require_permissions(super_admin_only=True))])

    Args:
        perm_code: 权限标识字符串 (如 'sys:role:list')，为 None 则不检查具体权限
        super_admin_only: 是否仅限超级管理员访问，为 True 时忽略 perm_code 参数

    Returns:
        依赖函数，返回当前用户对象
    """

    async def permission_dependency(current_user: User = Depends(get_current_user)):
        if is_super_admin(current_user):
            return current_user

        # 如果要求仅限超级管理员访问
        if super_admin_only:
            raise AuthorizationException("权限不足，仅限超级管理员访问")

        # 检查具体权限（如果提供了 perm_code）
        if perm_code:
            # 汇总当前用户所有权限（只考虑启用的角色）
            user_perms = set()
            for role in current_user.roles:
                if role.status == STATUS_ENABLED:
                    for menu in role.menus:
                        if menu.permission:
                            user_perms.add(menu.permission)
            if perm_code not in user_perms:
                raise AuthorizationException(f"缺少必要权限: {perm_code}")
        return current_user

    return permission_dependency
