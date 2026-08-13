"""角色服务测试：覆盖 get_role_menus 应返回「真叶子」菜单 ID。

返回叶子（不含父）的原因：NaiveUI NTree cascade 模式下，如果外部
checked-keys 包含父，会反向级联「父 checked → 所有当前子 checked」，
造成"全选"显示 bug。所以后端必须只返回叶子，让前端 NTree cascade
据此推导父状态（半选/全选/未选）。
"""

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    MENU_TYPE_BUTTON,
    MENU_TYPE_DIRECTORY,
    STATUS_ENABLED,
)
from app.db.base import role_menus
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.schemas.role import RoleQuery
from app.modules.system.service.role_service import role_service


def _make_menu(
    *,
    name: str,
    menu_type: str = MENU_TYPE_DIRECTORY,
    parent_id: int | None = 0,
    permission: str | None = None,
    route_name: str | None = None,
) -> Menu:
    return Menu(
        menu_name=name,
        menu_type=menu_type,
        parent_id=parent_id,
        order=0,
        status=STATUS_ENABLED,
        permission=permission,
        route_name=route_name,
        route_path=f"/{route_name}" if route_name else None,
    )


async def db_session_execute_insert(
    db: AsyncSession, role_id: int, menu_ids: list[int]
) -> None:
    await db.execute(
        insert(role_menus).values(
            [{"role_id": role_id, "menu_id": mid} for mid in menu_ids]
        )
    )
    await db.flush()


class TestGetRoleListDataScope:
    async def test_filters_roles_by_exact_data_scope(self, db_session: AsyncSession):
        name_prefix = "QA-data-scope-filter"
        all_role = Role(
            role_name=f"{name_prefix}-all",
            role_code="R_TEST_LIST_SCOPE_ALL",
            data_scope=DATA_SCOPE_ALL,
            status=STATUS_ENABLED,
        )
        custom_role = Role(
            role_name=f"{name_prefix}-custom",
            role_code="R_TEST_LIST_SCOPE_CUSTOM",
            data_scope=DATA_SCOPE_CUSTOM,
            status=STATUS_ENABLED,
        )
        db_session.add_all([all_role, custom_role])
        await db_session.flush()

        page = await role_service.get_role_list(
            db_session,
            RoleQuery(role_name=name_prefix, data_scope=DATA_SCOPE_CUSTOM),
        )

        assert page.total == 1
        assert [role.role_code for role in page.records] == ["R_TEST_LIST_SCOPE_CUSTOM"]


class TestGetRoleMenusReturnsLeaves:
    """get_role_menus 应只返回「真叶子」（menu 表中没有子的菜单）。"""

    async def test_returns_only_leaves_excludes_parents(self, db_session: AsyncSession):
        """角色拥有父+3 按钮，应只返回 3 个按钮（叶子），不返回父。"""
        parent = _make_menu(name="目录", route_name="test_dir")
        db_session.add(parent)
        await db_session.flush()

        buttons = []
        for i in range(3):
            b = _make_menu(
                name=f"b{i}",
                menu_type=MENU_TYPE_BUTTON,
                parent_id=parent.menu_id,
                permission=f"test:p{i}",
            )
            buttons.append(b)
        db_session.add_all(buttons)
        await db_session.flush()

        role = Role(role_name="R1", role_code="R_TEST_GET_1", status=STATUS_ENABLED)
        db_session.add(role)
        await db_session.flush()

        await db_session_execute_insert(
            db_session, role.role_id, [parent.menu_id, *[b.menu_id for b in buttons]]
        )

        result = await role_service.get_role_menus(db_session, role.role_id)
        result_set = set(result)

        # 关键断言：父被排除（否则前端 NTree cascade 会全选所有当前子）
        assert str(parent.menu_id) not in result_set, (
            f"父菜单不应返回，否则触发 NTree cascade 全选 bug。实际 = {result}"
        )
        # 3 个按钮（叶子）应返回
        for b in buttons:
            assert str(b.menu_id) in result_set
        assert len(result) == 3

    async def test_orphan_parent_not_returned(self, db_session: AsyncSession):
        """孤立父（role 拥有父 M1，M1 在 menu 表中有子但未被 role 拥有）不应返回。

        回归场景：旧 bug 让 role_menus 中只剩 (r, M1)，重新打开角色权限页
        时如果返回 [M1]，NTree cascade 会把 M1 当前所有子（含未被授权的）
        显示为全选。修复 A 后新数据不再产生孤立父，存量需管理员重配。
        """
        parent = _make_menu(name="孤立目录", route_name="test_orphan")
        db_session.add(parent)
        await db_session.flush()
        # 父下确实存在未被 role 拥有的子按钮
        unowned_btn = _make_menu(
            name="未关联按钮",
            menu_type=MENU_TYPE_BUTTON,
            parent_id=parent.menu_id,
            permission="test:orphan",
        )
        db_session.add(unowned_btn)
        await db_session.flush()

        role = Role(role_name="R2", role_code="R_TEST_GET_2", status=STATUS_ENABLED)
        db_session.add(role)
        await db_session.flush()

        # 只关联父，不关联任何子
        await db_session_execute_insert(db_session, role.role_id, [parent.menu_id])

        result = await role_service.get_role_menus(db_session, role.role_id)

        # 关键回归断言：孤立父必须被排除
        assert str(parent.menu_id) not in set(result), (
            f"孤立父不应返回，否则触发 NTree cascade 全选。实际 = {result}"
        )
        # 未关联子按钮也不应出现
        assert str(unowned_btn.menu_id) not in set(result)

    async def test_root_menu_without_children_is_leaf(self, db_session: AsyncSession):
        """根菜单（parent_id=NULL）若没有任何子，是真叶子，应被返回。

        回归测试：旧实现 NOT IN (subquery)，subquery 含 NULL 让整个查询失效。
        """
        root = _make_menu(name="NULL根", route_name="test_null_root")
        root.parent_id = None
        db_session.add(root)
        await db_session.flush()

        role = Role(role_name="R3", role_code="R_TEST_GET_3", status=STATUS_ENABLED)
        db_session.add(role)
        await db_session.flush()

        await db_session_execute_insert(db_session, role.role_id, [root.menu_id])

        result = await role_service.get_role_menus(db_session, role.role_id)

        # 关键回归断言：parent_id=NULL 不应让查询失效
        assert str(root.menu_id) in set(result), (
            f"NULL parent_id 让查询失效。实际 = {result}"
        )

    async def test_empty_role_returns_empty(self, db_session: AsyncSession):
        """无任何菜单关联的角色返回空列表。"""
        role = Role(role_name="R4", role_code="R_TEST_GET_4", status=STATUS_ENABLED)
        db_session.add(role)
        await db_session.flush()

        result = await role_service.get_role_menus(db_session, role.role_id)
        assert result == []

    async def test_parent_without_menu_children_returns_as_leaf(
        self, db_session: AsyncSession
    ):
        """菜单 C 没有任何子（无论是否被关联），是真叶子，应被返回。

        例如 AI 助手目录下原本有 AI 对话，但 AI 对话已被删除（孤儿目录），
        这时目录 C 在 menu 表中没有子，是真叶子。
        """
        leaf_dir = _make_menu(name="无子目录", route_name="test_no_child")
        db_session.add(leaf_dir)
        await db_session.flush()

        role = Role(role_name="R5", role_code="R_TEST_GET_5", status=STATUS_ENABLED)
        db_session.add(role)
        await db_session.flush()

        await db_session_execute_insert(db_session, role.role_id, [leaf_dir.menu_id])

        result = await role_service.get_role_menus(db_session, role.role_id)

        assert str(leaf_dir.menu_id) in set(result)
