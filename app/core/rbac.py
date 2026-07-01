"""RBAC 共用判定：超管身份识别。

抽出独立模块避免 core.auth 与 utils.data_scope 之间双份实现漂移。
"""

from app.constants import ADMIN_USERNAME, STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.modules.system.models.user import User


def is_super_admin(user: User) -> bool:
    """判断用户是否拥有超管身份。

    判定规则：
    1. user_name == ADMIN_USERNAME → 永远超管（admin 账号本身）
    2. 含有"启用的" R_SUPER 角色 → 超管

    status 过滤纳入这里，让 require_permissions 和 data_scope 共用
    同一语义，避免禁用 R_SUPER 角色后两处行为不一致。
    """
    if user.user_name == ADMIN_USERNAME:
        return True
    return any(
        r.role_code == SUPER_ADMIN_ROLE_CODE and r.status == STATUS_ENABLED
        for r in user.roles
    )
