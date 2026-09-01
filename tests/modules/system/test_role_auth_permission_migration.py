"""Phase 2 user role-delegation permission compatibility matrix."""

from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_DISABLED, STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.id_generator import next_id
from app.db.base import role_menus
from app.modules.system.constants import USER_ROLE_AUTH_PERMISSION
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from scripts.migrate_phase2_authorization import migrate_phase2_authorization

WRITER_PERMISSIONS = (
    "system:user:add",
    "system:user:edit",
    "system:user:import",
)


async def _permission_menu(
    db: AsyncSession,
    permission: str,
    *,
    status: str = STATUS_ENABLED,
) -> Menu:
    menu = await db.scalar(
        select(Menu).where(Menu.tenant_id == 0, Menu.permission == permission)
    )
    if menu is None:
        menu = Menu(
            tenant_id=0,
            menu_id=next_id(),
            menu_name=f"P2 {permission}",
            menu_type="F",
            permission=permission,
            status=status,
        )
        db.add(menu)
        await db.flush()
    else:
        menu.status = status
    return menu


async def test_upgrade_grants_each_historical_writer_without_expanding_entry(
    db_session: AsyncSession,
) -> None:
    marker = next_id()
    writer_menus = [
        await _permission_menu(
            db_session,
            permission,
            status=STATUS_DISABLED if permission.endswith(":add") else STATUS_ENABLED,
        )
        for permission in WRITER_PERMISSIONS
    ]
    roles: list[Role] = []
    for index, menu in enumerate(writer_menus):
        role = Role(
            tenant_id=0,
            role_id=next_id(),
            role_name=f"P2 writer {marker} {index}",
            role_code=f"R_P2_WRITER_{marker}_{index}",
            status=STATUS_ENABLED,
        )
        role.menus = [menu]
        roles.append(role)
    unrelated = Role(
        tenant_id=0,
        role_id=next_id(),
        role_name=f"P2 unrelated {marker}",
        role_code=f"R_P2_UNRELATED_{marker}",
        status=STATUS_ENABLED,
    )
    db_session.add_all([*roles, unrelated])
    await db_session.flush()

    with patch(
        "scripts.migrate_phase2_authorization.authorization_lock_service.lock_authorization_migration",
        AsyncMock(),
    ) as migration_lock:
        first = await migrate_phase2_authorization(db_session)
        second = await migrate_phase2_authorization(db_session)

    assert migration_lock.await_count == 2

    role_auth_menu = await db_session.scalar(
        select(Menu).where(
            Menu.tenant_id == 0,
            Menu.permission == USER_ROLE_AUTH_PERMISSION,
        )
    )
    assert role_auth_menu is not None
    granted_role_ids = set(
        (
            await db_session.execute(
                select(role_menus.c.role_id).where(
                    role_menus.c.tenant_id == 0,
                    role_menus.c.menu_id == role_auth_menu.menu_id,
                )
            )
        ).scalars()
    )
    super_role_id = await db_session.scalar(
        select(Role.role_id).where(
            Role.tenant_id == 0,
            Role.role_code == SUPER_ADMIN_ROLE_CODE,
        )
    )
    assert {role.role_id for role in roles} <= granted_role_ids
    assert super_role_id in granted_role_ids
    assert unrelated.role_id not in granted_role_ids
    assert first.roles_granted >= len(roles)
    assert second.roles_granted == 0

    for role, original_permission in zip(roles, WRITER_PERMISSIONS, strict=True):
        permissions = set(
            (
                await db_session.execute(
                    select(Menu.permission)
                    .join(
                        role_menus,
                        (role_menus.c.tenant_id == Menu.tenant_id)
                        & (role_menus.c.menu_id == Menu.menu_id),
                    )
                    .where(
                        role_menus.c.tenant_id == 0,
                        role_menus.c.role_id == role.role_id,
                    )
                )
            ).scalars()
        )
        assert original_permission in permissions
        assert USER_ROLE_AUTH_PERMISSION in permissions
        assert not (set(WRITER_PERMISSIONS) - {original_permission}) & permissions


async def test_upgrade_creates_permission_under_user_menu(
    db_session: AsyncSession,
) -> None:
    await migrate_phase2_authorization(db_session)

    permission_menu = await db_session.scalar(
        select(Menu).where(
            Menu.tenant_id == 0,
            Menu.permission == USER_ROLE_AUTH_PERMISSION,
        )
    )
    assert permission_menu is not None
    parent = await db_session.get(Menu, permission_menu.parent_id)

    assert permission_menu.menu_type == "F"
    assert permission_menu.status == STATUS_ENABLED
    assert parent is not None
    assert parent.route_name == "system_user"
