"""权限检查装饰器"""

import logging

from fastapi import Depends

from app.constants import ADMIN_USERNAME, STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.exceptions import AuthorizationException
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)


def is_super_admin(user: User) -> bool:
    """判断用户是否为超级管理员"""
    return user.user_name == ADMIN_USERNAME or SUPER_ADMIN_ROLE_CODE in [
        r.role_code for r in user.roles
    ]


def require_permissions(perm_code: str = None, super_admin_only: bool = False):
    """
    权限校验装饰器

    用法示例:
        @router.get("/list", dependencies=[Depends(require_permissions("system:user:list"))])
        @router.get("/admin-only", dependencies=[Depends(require_permissions(super_admin_only=True))])

    Args:
        perm_code: 权限标识字符串 (如 'system:role:list')，为 None 则不检查具体权限
        super_admin_only: 是否仅限超级管理员访问，为 True 时忽略 perm_code 参数

    Returns:
        依赖函数，返回当前用户对象

    设计：
        - msg 保持通用（"权限不足"），不向终端用户暴露内部权限码
        - 具体缺失的权限码、用户已有权限集写入 INFO 日志，admin 在日志查
        - errorCode（MISSING_PERMISSION / SUPER_ADMIN_ONLY）给前端 i18n 映射
    """

    async def permission_dependency(current_user: User = Depends(get_current_user)):
        if is_super_admin(current_user):
            return current_user

        # 如果要求仅限超级管理员访问
        if super_admin_only:
            logger.info(
                "Permission denied (super_admin_only): user=%s",
                current_user.user_name,
            )
            raise AuthorizationException(
                "权限不足",
                error_code="SUPER_ADMIN_ONLY",
            )

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
                # 详细权限信息进日志，方便 admin 排查；msg 保持通用
                logger.info(
                    "Permission denied: user=%s required=%s has=%s",
                    current_user.user_name,
                    perm_code,
                    sorted(user_perms),
                )
                raise AuthorizationException(
                    "权限不足",
                    error_code="MISSING_PERMISSION",
                )
        return current_user

    return permission_dependency
