"""Explicit writers for tenant-owned many-to-many association rows."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.core.tenant import TenantContext
from app.db.base import role_depts, role_menus, user_roles
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User


def _require_same_tenant(
    values: Sequence[Any], *, tenant: TenantContext, resource: str
) -> None:
    if any(int(value.tenant_id) != tenant.tenant_id for value in values):
        raise ValueError(f"{resource} association crosses tenant boundary")


async def replace_user_roles(
    db: AsyncSession,
    user: User,
    roles: Sequence[Role],
    *,
    tenant: TenantContext,
) -> None:
    """Replace one user's role links with explicit tenant-owned rows."""
    _require_same_tenant((user, *roles), tenant=tenant, resource="user-role")
    await db.execute(
        delete(user_roles).where(
            user_roles.c.tenant_id == tenant.tenant_id,
            user_roles.c.user_id == user.user_id,
        )
    )
    if roles:
        await db.execute(
            insert(user_roles),
            [
                {
                    "tenant_id": tenant.tenant_id,
                    "user_id": user.user_id,
                    "role_id": role.role_id,
                }
                for role in roles
            ],
        )
    set_committed_value(user, "roles", list(roles))


async def replace_role_depts(
    db: AsyncSession,
    role: Role,
    depts: Sequence[Dept],
    *,
    tenant: TenantContext,
) -> None:
    """Replace one role's custom data-scope departments."""
    _require_same_tenant((role, *depts), tenant=tenant, resource="role-dept")
    await db.execute(
        delete(role_depts).where(
            role_depts.c.tenant_id == tenant.tenant_id,
            role_depts.c.role_id == role.role_id,
        )
    )
    if depts:
        await db.execute(
            insert(role_depts),
            [
                {
                    "tenant_id": tenant.tenant_id,
                    "role_id": role.role_id,
                    "dept_id": dept.dept_id,
                }
                for dept in depts
            ],
        )
    set_committed_value(role, "depts", list(depts))


async def replace_role_menus(
    db: AsyncSession,
    role: Role,
    menus: Sequence[Menu],
    *,
    tenant: TenantContext,
) -> None:
    """Replace one role's menu links with explicit tenant-owned rows."""
    _require_same_tenant((role, *menus), tenant=tenant, resource="role-menu")
    await db.execute(
        delete(role_menus).where(
            role_menus.c.tenant_id == tenant.tenant_id,
            role_menus.c.role_id == role.role_id,
        )
    )
    if menus:
        await db.execute(
            insert(role_menus),
            [
                {
                    "tenant_id": tenant.tenant_id,
                    "role_id": role.role_id,
                    "menu_id": menu.menu_id,
                }
                for menu in menus
            ],
        )
    set_committed_value(role, "menus", list(menus))
