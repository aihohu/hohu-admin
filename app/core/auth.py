from fastapi import Depends, HTTPException, status

from app.core.exceptions import AuthorizationException
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User


def check_permissions(required_perm: str):
    """
    权限检查装饰器工厂

    用法:
        @router.get("/data", dependencies=[Depends(check_permissions("sys:user:list"))])
    """

    async def permission_dependency(current_user: User = Depends(get_current_user)):
        # 汇总当前用户所有权限
        user_perms = {
            m.permission for r in current_user.roles for m in r.menus if m.permission
        }

        # 如果不是超级管理员且没有所需权限
        if (
            "admin" not in [r.role_code for r in current_user.roles]
            and required_perm not in user_perms
        ):
            raise AuthorizationException(f"缺少权限: {required_perm}")
        return True

    return permission_dependency


def require_permissions(perm_code: str = None, super_admin_only: bool = False):
    """
    权限校验装饰器
    :param perm_code: 权限标识字符串 (如 'sys:role:list')
    :param super_admin_only: 是否仅限超级管理员
    """

    async def permission_dependency(current_user: User = Depends(get_current_user)):
        # 1. 优先判断是否是超级管理员字段
        if current_user.is_admin:
            return current_user

        if super_admin_only and not current_user.is_admin:
            raise AuthorizationException("权限不足，仅限超级管理员访问")

        # 2. 判断是否拥有具体的权限标识 (需要查询关联的 roles -> menus)
        # 这里假设 User 模型已经预加载了 roles.menus
        user_perms = set()
        for role in current_user.roles:
            if role.status == "1":  # 只有启用的角色才计算权限
                for menu in role.menus:
                    if menu.permission:
                        user_perms.add(menu.permission)

        if perm_code and perm_code not in user_perms:
            raise AuthorizationException(f"缺少必要权限: {perm_code}")

        return current_user

    return permission_dependency
