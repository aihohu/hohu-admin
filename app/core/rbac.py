"""RBAC 共用判定：超管身份识别。

抽出独立模块避免 core.auth 与 utils.data_scope 之间双份实现漂移。
"""

from app.constants import STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.tenant import DEFAULT_TENANT_ID
from app.modules.system.models.user import User


def is_super_admin(user: User) -> bool:
    """判断用户是否拥有超管身份。

    判定规则：
    含有启用的 R_SUPER 角色，权限限定在用户所属租户。

    status 过滤纳入这里，让 require_permissions 和 data_scope 共用
    同一语义，避免禁用 R_SUPER 角色后两处行为不一致。
    """
    return any(
        r.role_code == SUPER_ADMIN_ROLE_CODE and r.status == STATUS_ENABLED
        for r in user.roles
    )


def is_system_admin(user: User) -> bool:
    """Only the enabled system-scope super role grants global administration.

    Account names are not authority. A renamed administrator retains its role;
    an unprivileged account named admin never acquires global access.
    """
    return (
        getattr(user, "tenant_id", None) == DEFAULT_TENANT_ID
        and getattr(user, "status", None) == STATUS_ENABLED
        and any(
            getattr(role, "tenant_id", None) == DEFAULT_TENANT_ID
            and role.role_code == SUPER_ADMIN_ROLE_CODE
            and role.status == STATUS_ENABLED
            for role in user.roles
        )
    )
