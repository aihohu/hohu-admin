"""菜单服务测试：覆盖按钮增删时不应破坏 role_menus 关联。"""

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import MENU_TYPE_BUTTON, MENU_TYPE_DIRECTORY, STATUS_ENABLED
from app.db.base import role_menus
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.schemas.menu import ButtonCreate, MenuUpdate
from app.modules.system.service.menu_service import menu_service
from tests.tenant_helpers import tenant_context


@pytest.fixture
async def parent_menu(db_session: AsyncSession) -> Menu:
    """创建一个目录菜单作为父。"""
    menu = Menu(
        tenant_id=0,
        menu_name="测试目录",
        menu_type=MENU_TYPE_DIRECTORY,
        parent_id=0,
        order=1,
        status=STATUS_ENABLED,
        route_name="test_parent",
        route_path="/test-parent",
    )
    db_session.add(menu)
    await db_session.flush()
    return menu


@pytest.fixture
async def role_with_buttons(
    db_session: AsyncSession, parent_menu: Menu
) -> tuple[Role, list[Menu]]:
    """创建一个角色，并关联 3 个按钮权限（p1/p2/p3）。

    返回 (role, buttons)，buttons 是已关联的按钮列表。
    直接 INSERT role_menus 关联表，绕开 ORM 反向同步触发的 async lazy load。
    """
    role = Role(
        tenant_id=0,
        role_name="测试角色",
        role_code="R_TEST_PERM",
        status=STATUS_ENABLED,
    )
    db_session.add(role)

    buttons = []
    for code in ["test:p1", "test:p2", "test:p3"]:
        btn = Menu(
            tenant_id=0,
            menu_name=f"按钮 {code}",
            permission=code,
            menu_type=MENU_TYPE_BUTTON,
            parent_id=parent_menu.menu_id,
            order=0,
            status=STATUS_ENABLED,
        )
        buttons.append(btn)
    db_session.add_all(buttons)
    await db_session.flush()

    await db_session.execute(
        insert(role_menus).values(
            [
                {
                    "tenant_id": 0,
                    "role_id": role.role_id,
                    "menu_id": parent_menu.menu_id,
                },
                *[
                    {"tenant_id": 0, "role_id": role.role_id, "menu_id": b.menu_id}
                    for b in buttons
                ],
            ]
        )
    )
    await db_session.flush()
    return role, buttons


def _button(code: str, desc: str = "") -> ButtonCreate:
    return ButtonCreate(code=code, desc=desc or f"按钮 {code}")


class TestUpdateMenuButtonsPreserveAssociation:
    """update_menu 修改按钮时必须保留已有按钮的 menu_id 与 role_menus 关联。"""

    async def test_adding_button_keeps_existing_button_ids(
        self, db_session: AsyncSession, parent_menu: Menu, role_with_buttons
    ):
        """新增按钮时，已有按钮的 menu_id 不应改变。"""
        _, buttons = role_with_buttons
        original_ids = {b.permission: b.menu_id for b in buttons}

        menu_in = MenuUpdate(
            menu_name=parent_menu.menu_name,
            menu_type=MENU_TYPE_DIRECTORY,
            parent_id=0,
            order=1,
            status=STATUS_ENABLED,
            route_name="test_parent",
            route_path="/test-parent",
            buttons=[
                _button("test:p1"),
                _button("test:p2"),
                _button("test:p3"),
                _button("test:p4"),
            ],
        )

        await menu_service.update_menu(
            db_session,
            parent_menu.menu_id,
            menu_in,
            tenant=tenant_context(tenant_id=0),
        )
        await db_session.flush()

        result = await db_session.execute(
            select(Menu).where(
                Menu.tenant_id == 0,
                Menu.parent_id == parent_menu.menu_id,
                Menu.menu_type == MENU_TYPE_BUTTON,
            )
        )
        new_buttons = {b.permission: b for b in result.scalars().all()}

        assert set(new_buttons) == {"test:p1", "test:p2", "test:p3", "test:p4"}
        # 关键断言：旧按钮的 menu_id 必须保留
        assert new_buttons["test:p1"].menu_id == original_ids["test:p1"]
        assert new_buttons["test:p2"].menu_id == original_ids["test:p2"]
        assert new_buttons["test:p3"].menu_id == original_ids["test:p3"]
        # 新按钮获得新 ID
        assert new_buttons["test:p4"].menu_id != original_ids.get("test:p4")

    async def test_adding_button_preserves_role_menus(
        self, db_session: AsyncSession, parent_menu: Menu, role_with_buttons
    ):
        """新增按钮时，已有按钮的 role_menus 关联必须保留。"""
        role, buttons = role_with_buttons
        original_button_ids = [b.menu_id for b in buttons]

        menu_in = MenuUpdate(
            menu_name=parent_menu.menu_name,
            menu_type=MENU_TYPE_DIRECTORY,
            parent_id=0,
            order=1,
            status=STATUS_ENABLED,
            route_name="test_parent",
            route_path="/test-parent",
            buttons=[
                _button("test:p1"),
                _button("test:p2"),
                _button("test:p3"),
                _button("test:p4"),
            ],
        )

        await menu_service.update_menu(
            db_session,
            parent_menu.menu_id,
            menu_in,
            tenant=tenant_context(tenant_id=0),
        )
        await db_session.flush()

        # 关键断言：3 个旧按钮在 role_menus 中依然存在
        result = await db_session.execute(
            select(role_menus.c.menu_id).where(
                role_menus.c.tenant_id == 0,
                role_menus.c.role_id == role.role_id,
            )
        )
        associated_ids = set(result.scalars().all())

        for old_id in original_button_ids:
            assert old_id in associated_ids, (
                f"button {old_id} 不应丢失 role_menus 关联，但实际关联 = {associated_ids}"
            )

    async def test_removing_button_cascades_role_menus(
        self, db_session: AsyncSession, parent_menu: Menu, role_with_buttons
    ):
        """删除按钮时，对应 role_menus 关联应被 CASCADE 清除（这是预期行为）。"""
        role, buttons = role_with_buttons
        removed_id = buttons[0].menu_id  # test:p1
        kept_ids = [b.menu_id for b in buttons[1:]]  # test:p2, test:p3

        menu_in = MenuUpdate(
            menu_name=parent_menu.menu_name,
            menu_type=MENU_TYPE_DIRECTORY,
            parent_id=0,
            order=1,
            status=STATUS_ENABLED,
            route_name="test_parent",
            route_path="/test-parent",
            buttons=[_button("test:p2"), _button("test:p3")],
        )

        await menu_service.update_menu(
            db_session,
            parent_menu.menu_id,
            menu_in,
            tenant=tenant_context(tenant_id=0),
        )
        await db_session.flush()

        result = await db_session.execute(
            select(role_menus.c.menu_id).where(
                role_menus.c.tenant_id == 0,
                role_menus.c.role_id == role.role_id,
            )
        )
        associated_ids = set(result.scalars().all())

        assert removed_id not in associated_ids, "被删除的按钮关联应清掉"
        for kid in kept_ids:
            assert kid in associated_ids, "保留按钮的关联不应丢失"

    async def test_renaming_button_keeps_menu_id(
        self, db_session: AsyncSession, parent_menu: Menu, role_with_buttons
    ):
        """只改按钮 desc（permission 不变）时，menu_id 应保留。"""
        _, buttons = role_with_buttons
        original_id = buttons[0].menu_id

        menu_in = MenuUpdate(
            menu_name=parent_menu.menu_name,
            menu_type=MENU_TYPE_DIRECTORY,
            parent_id=0,
            order=1,
            status=STATUS_ENABLED,
            route_name="test_parent",
            route_path="/test-parent",
            buttons=[
                _button("test:p1", desc="新描述1"),
                _button("test:p2"),
                _button("test:p3"),
            ],
        )

        await menu_service.update_menu(
            db_session,
            parent_menu.menu_id,
            menu_in,
            tenant=tenant_context(tenant_id=0),
        )
        await db_session.flush()

        result = await db_session.execute(
            select(Menu).where(
                Menu.tenant_id == 0,
                Menu.parent_id == parent_menu.menu_id,
                Menu.menu_type == MENU_TYPE_BUTTON,
                Menu.permission == "test:p1",
            )
        )
        renamed = result.scalars().first()
        assert renamed is not None
        assert renamed.menu_id == original_id
        assert renamed.menu_name == "新描述1"
