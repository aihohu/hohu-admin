"""Phase 3 additive department-move permission migration tests."""

import importlib
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import DATA_SCOPE_SELF, STATUS_ENABLED
from app.core.id_generator import next_id
from app.db.base import role_menus
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role


async def test_upgrade_grants_move_only_to_super_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    migration = importlib.import_module("scripts.migrate_phase3_dept_authorization")
    marker = next_id()
    parent = Menu(
        menu_id=next_id(),
        parent_id=0,
        route_name=f"phase3_dept_parent_{marker}",
        menu_name=f"Phase 3 department parent {marker}",
        menu_type="C",
        status=STATUS_ENABLED,
    )
    edit_menu = Menu(
        menu_id=next_id(),
        parent_id=parent.menu_id,
        menu_name=f"Phase 3 department edit {marker}",
        menu_type="F",
        permission="system:dept:edit",
        status=STATUS_ENABLED,
    )
    super_role = Role(
        role_id=next_id(),
        role_name=f"Phase 3 super {marker}",
        role_code=f"R_PHASE3_SUPER_{marker}",
        data_scope=DATA_SCOPE_SELF,
        status=STATUS_ENABLED,
    )
    editor_role = Role(
        role_id=next_id(),
        role_name=f"Phase 3 editor {marker}",
        role_code=f"R_PHASE3_EDITOR_{marker}",
        data_scope=DATA_SCOPE_SELF,
        status=STATUS_ENABLED,
        menus=[edit_menu],
    )
    await db_session.execute(delete(Menu).where(Menu.permission == "system:dept:move"))
    db_session.add_all([parent, edit_menu, super_role, editor_role])
    await db_session.flush()

    with (
        patch.object(migration, "SUPER_ADMIN_ROLE_CODE", super_role.role_code),
        patch.object(migration, "DEPT_PARENT_ROUTE", parent.route_name),
        patch.object(
            migration.authorization_lock_service,
            "lock_authorization_migration",
            AsyncMock(),
        ) as migration_lock,
    ):
        first = await migration.migrate_phase3_dept_authorization(db_session)
        second = await migration.migrate_phase3_dept_authorization(db_session)

    move_menu = await db_session.scalar(
        select(Menu).where(Menu.permission == "system:dept:move")
    )
    assert move_menu is not None
    assert move_menu.parent_id == parent.menu_id
    granted_role_ids = set(
        (
            await db_session.execute(
                select(role_menus.c.role_id).where(
                    role_menus.c.menu_id == move_menu.menu_id
                )
            )
        ).scalars()
    )
    assert super_role.role_id in granted_role_ids
    assert editor_role.role_id not in granted_role_ids
    assert first.menu_created is True
    assert first.roles_granted == 1
    assert second.menu_created is False
    assert second.roles_granted == 0
    assert migration_lock.await_count == 2
