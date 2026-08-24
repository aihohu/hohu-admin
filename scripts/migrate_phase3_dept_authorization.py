# ruff: noqa: T201
"""Idempotently seed the Phase 3 department move permission."""

import asyncio
from dataclasses import dataclass

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.constants import STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.config import settings
from app.core.id_generator import next_id
from app.db.base import role_menus
from app.modules.system.constants import DEPT_MOVE_PERMISSION
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.service.authorization_lock import authorization_lock_service

DEPT_PARENT_ROUTE = "system_dept"


@dataclass(frozen=True)
class Phase3DeptAuthorizationMigrationResult:
    """Observable additive migration outcome."""

    menu_created: bool
    roles_granted: int


async def _ensure_move_menu(db: AsyncSession) -> tuple[Menu, bool]:
    menu = await db.scalar(select(Menu).where(Menu.permission == DEPT_MOVE_PERMISSION))
    if menu is not None:
        return menu, False
    parent = await db.scalar(select(Menu).where(Menu.route_name == DEPT_PARENT_ROUTE))
    if parent is None:
        raise RuntimeError("system_dept parent menu not found; run menu seed first")
    menu = Menu(
        menu_id=next_id(),
        parent_id=parent.menu_id,
        menu_name="移动",
        menu_type="F",
        permission=DEPT_MOVE_PERMISSION,
        status=STATUS_ENABLED,
    )
    db.add(menu)
    await db.flush()
    return menu, True


async def _grant_super_role(db: AsyncSession, *, menu_id: int) -> int:
    role_ids = tuple(
        int(role_id)
        for role_id in (
            await db.execute(
                select(Role.role_id).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
            )
        ).scalars()
    )
    if not role_ids:
        return 0
    existing = {
        int(role_id)
        for role_id in (
            await db.execute(
                select(role_menus.c.role_id).where(
                    role_menus.c.role_id.in_(role_ids),
                    role_menus.c.menu_id == menu_id,
                )
            )
        ).scalars()
    }
    missing = sorted(set(role_ids) - existing)
    if missing:
        await db.execute(
            insert(role_menus),
            [{"role_id": role_id, "menu_id": menu_id} for role_id in missing],
        )
    return len(missing)


async def migrate_phase3_dept_authorization(
    db: AsyncSession,
) -> Phase3DeptAuthorizationMigrationResult:
    """Apply the additive permission migration without committing."""
    await authorization_lock_service.lock_authorization_migration(db)
    menu, menu_created = await _ensure_move_menu(db)
    roles_granted = await _grant_super_role(db, menu_id=int(menu.menu_id))
    return Phase3DeptAuthorizationMigrationResult(
        menu_created=menu_created,
        roles_granted=roles_granted,
    )


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with async_session() as db:
            async with db.begin():
                result = await migrate_phase3_dept_authorization(db)
        print(
            "Phase 3 department authorization migration complete: "
            f"menu_created={result.menu_created}, "
            f"roles_granted={result.roles_granted}"
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
