# ruff: noqa: T201
"""Idempotently seed Phase 2 delegation permission compatibility."""

import asyncio
from dataclasses import dataclass

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.constants import STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.config import settings
from app.core.id_generator import next_id
from app.core.tenant import DEFAULT_TENANT_ID
from app.db.base import role_menus
from app.modules.system.constants import USER_ROLE_AUTH_PERMISSION
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.service.authorization_lock import (
    authorization_lock_service,
)

HISTORICAL_USER_ROLE_WRITER_PERMISSIONS = frozenset(
    {
        "system:user:add",
        "system:user:edit",
        "system:user:import",
    }
)


@dataclass(frozen=True)
class Phase2AuthorizationMigrationResult:
    menu_created: bool
    roles_granted: int


async def _ensure_role_auth_menu(db: AsyncSession) -> tuple[Menu, bool]:
    existing = await db.scalar(
        select(Menu).where(
            Menu.tenant_id == DEFAULT_TENANT_ID,
            Menu.permission == USER_ROLE_AUTH_PERMISSION,
        )
    )
    if existing is not None:
        return existing, False
    parent = await db.scalar(
        select(Menu).where(
            Menu.tenant_id == DEFAULT_TENANT_ID,
            Menu.route_name == "system_user",
        )
    )
    if parent is None:
        raise RuntimeError("system_user parent menu not found; run menu seed first")
    menu = Menu(
        tenant_id=DEFAULT_TENANT_ID,
        menu_id=next_id(),
        parent_id=parent.menu_id,
        menu_name="角色授权",
        menu_type="F",
        permission=USER_ROLE_AUTH_PERMISSION,
        status=STATUS_ENABLED,
    )
    db.add(menu)
    await db.flush()
    return menu, True


async def _compatible_role_ids(db: AsyncSession) -> set[int]:
    writer_role_ids = {
        int(role_id)
        for role_id in (
            await db.execute(
                select(role_menus.c.role_id)
                .join(
                    Menu,
                    (Menu.tenant_id == role_menus.c.tenant_id)
                    & (Menu.menu_id == role_menus.c.menu_id),
                )
                .where(
                    role_menus.c.tenant_id == DEFAULT_TENANT_ID,
                    Menu.tenant_id == DEFAULT_TENANT_ID,
                    Menu.permission.in_(HISTORICAL_USER_ROLE_WRITER_PERMISSIONS),
                )
                .distinct()
            )
        ).scalars()
    }
    writer_role_ids.update(
        int(role_id)
        for role_id in (
            await db.execute(
                select(Role.role_id).where(
                    Role.tenant_id == DEFAULT_TENANT_ID,
                    Role.role_code == SUPER_ADMIN_ROLE_CODE,
                )
            )
        ).scalars()
    )
    return writer_role_ids


async def _grant_role_auth(
    db: AsyncSession,
    *,
    menu_id: int,
    role_ids: set[int],
) -> int:
    if not role_ids:
        return 0
    existing = {
        int(role_id)
        for role_id in (
            await db.execute(
                select(role_menus.c.role_id).where(
                    role_menus.c.tenant_id == DEFAULT_TENANT_ID,
                    role_menus.c.role_id.in_(role_ids),
                    role_menus.c.menu_id == menu_id,
                )
            )
        ).scalars()
    }
    missing = role_ids - existing
    if missing:
        await db.execute(
            insert(role_menus),
            [
                {
                    "tenant_id": DEFAULT_TENANT_ID,
                    "role_id": role_id,
                    "menu_id": menu_id,
                }
                for role_id in sorted(missing)
            ],
        )
    return len(missing)


async def migrate_phase2_authorization(
    db: AsyncSession,
) -> Phase2AuthorizationMigrationResult:
    """Apply the additive compatibility migration without committing."""
    await authorization_lock_service.lock_authorization_migration(db)
    menu, menu_created = await _ensure_role_auth_menu(db)
    roles_granted = await _grant_role_auth(
        db,
        menu_id=int(menu.menu_id),
        role_ids=await _compatible_role_ids(db),
    )
    return Phase2AuthorizationMigrationResult(
        menu_created=menu_created,
        roles_granted=roles_granted,
    )


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with async_session() as db:
            async with db.begin():
                result = await migrate_phase2_authorization(db)
        print(
            "Phase 2 authorization migration complete: "
            f"menu_created={result.menu_created}, "
            f"roles_granted={result.roles_granted}"
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
