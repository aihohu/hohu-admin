"""Narrow user-list facts read under the tenant barrier."""

from collections import defaultdict

from sqlalchemy import and_, select

from app.constants import STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.exceptions import AuthenticationException, AuthorizationException
from app.core.field_policy.contracts import limit
from app.core.tenant import TenantContext
from app.db.base import role_menus, user_roles
from app.modules.system.field_policy.list_admission import RoleFacts, list_engine
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User


async def load_list_identity(db, tx, claims, catalog, expected_tenant_code):
    tx.assert_session(db)
    tx.assert_barrier(claims.tenant_id)
    actor = (
        await db.execute(
            select(
                User.user_id,
                User.tenant_id,
                User.status,
                User.auth_version,
                Tenant.tenant_code,
                Tenant.row_version,
                Tenant.status.label("tenant_status"),
            )
            .join(Tenant, Tenant.tenant_id == User.tenant_id)
            .where(User.tenant_id == claims.tenant_id, User.user_id == claims.user_id)
        )
    ).one_or_none()
    stale = (
        actor is None
        or actor.auth_version != claims.user_version
        or actor.row_version != claims.tenant_version
        or expected_tenant_code is not None
        and actor.tenant_code != expected_tenant_code
    )
    if expected_tenant_code is not None and (
        stale or actor.status != STATUS_ENABLED or actor.tenant_status != STATUS_ENABLED
    ):
        raise AuthenticationException(
            "当前身份已经失效", error_code="INVALID_CREDENTIALS"
        )
    if stale:
        raise AuthenticationException("Token 无效或已过期", error_code="TOKEN_EXPIRED")
    if actor.tenant_status != STATUS_ENABLED:
        raise AuthorizationException("租户已被禁用", error_code="TENANT_DISABLED")
    if actor.status != STATUS_ENABLED:
        raise AuthorizationException("账号已被禁用", error_code="ACCOUNT_DISABLED")
    tenant = TenantContext(
        actor.tenant_id,
        actor.tenant_code,
        actor.user_id,
        actor.row_version,
        "access_token",
    )
    actions = (
        "user.list-read",
        "user.create",
        "user.update",
        "user.roles",
        "user.departments",
    )
    codes = {p for action in actions for p in catalog.actions[action].permission_codes}
    functions = role_menus.join(
        Menu,
        and_(
            Menu.tenant_id == role_menus.c.tenant_id,
            Menu.menu_id == role_menus.c.menu_id,
            Menu.permission.in_(codes),
        ),
    )
    rows = await db.execute(
        select(
            Role.role_id,
            Role.tenant_id,
            Role.role_code,
            Role.status,
            Role.data_scope,
            Menu.permission,
        )
        .join(
            user_roles,
            and_(
                user_roles.c.tenant_id == Role.tenant_id,
                user_roles.c.role_id == Role.role_id,
            ),
        )
        .outerjoin(
            functions,
            and_(
                role_menus.c.role_id == Role.role_id,
                role_menus.c.tenant_id == Role.tenant_id,
            ),
        )
        .where(
            Role.tenant_id == tenant.tenant_id,
            user_roles.c.user_id == actor.user_id,
            Role.status == STATUS_ENABLED,
        )
        .distinct()
        .order_by(Role.role_id)
    )
    roles, role_permissions = {}, defaultdict(set)
    permissions = set()
    for row in rows:
        roles[row.role_id] = row
        if row.permission:
            role_permissions[row.role_id].add(row.permission)
            permissions.add(row.permission)
    limit(len(roles) <= 1024)
    admitted = []
    engine = list_engine()
    for rid, role in roles.items():
        super_admin = role.role_code == SUPER_ADMIN_ROLE_CODE
        if super_admin:
            permissions.update(codes)
        facts = RoleFacts(
            role.tenant_id, True, super_admin, frozenset(role_permissions[rid])
        )
        if engine.enforce(tenant.tenant_id, facts, "system:user:list"):
            admitted.append(role)
    if not admitted:
        raise AuthorizationException("缺少操作权限", error_code="MISSING_PERMISSION")
    return tenant, actor, admitted, frozenset(permissions)
