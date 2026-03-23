from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import MENU_TYPE_BUTTON, MENU_TYPE_MENU, STATUS_ENABLED
from app.core.exceptions import (
    BusinessRuleException,
    InvalidParameterException,
    MenuNotFoundException,
)
from app.modules.system.models.menu import Menu
from app.modules.system.schemas.menu import MenuCreate, MenuQuery, MenuUpdate
from app.utils.pagination import paginate


class MenuService:
    """菜单业务逻辑服务"""

    async def get_menu_list(self, db: AsyncSession, query: MenuQuery):
        """
        获取菜单分页列表

        Args:
            db: 数据库会话
            query: 查询参数

        Returns:
            分页数据对象
        """
        # 使用通用分页查询
        page_data = await paginate(
            db=db,
            model=Menu,
            query_params=query,
            order_by=Menu.order.asc(),
        )

        return page_data

    async def get_all_menus(self, db: AsyncSession) -> list[Menu]:
        """
        获取所有启用的菜单列表（不分页）

        Args:
            db: 数据库会话

        Returns:
            菜单列表
        """
        stmt = (
            select(Menu).where(Menu.status == STATUS_ENABLED).order_by(Menu.order.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_all_pages(self, db: AsyncSession) -> list[str]:
        """
        获取所有页面路由名称

        Args:
            db: 数据库会话

        Returns:
            页面路由名称列表
        """
        stmt = (
            select(Menu.route_name)
            .where(
                Menu.status == STATUS_ENABLED,
                Menu.menu_type == MENU_TYPE_MENU,
            )
            .order_by(Menu.order.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def create_menu(self, db: AsyncSession, menu_in: MenuCreate) -> Menu:
        """
        创建新菜单

        Args:
            db: 数据库会话
            menu_in: 菜单创建数据

        Returns:
            创建的菜单对象
        """
        # 排除 buttons 和 query 字段，这些不是 Menu 模型的字段
        menu_data = menu_in.model_dump(exclude={"buttons", "query"})
        new_menu = Menu(**menu_data)
        db.add(new_menu)

        # 如果有按钮，需要保存到 flush 后获取 menu_id
        await db.flush()

        # 批量添加按钮
        if menu_in.buttons:
            new_buttons = []
            for btn in menu_in.buttons:
                button_menu = Menu(
                    menu_name=btn.desc,
                    permission=btn.code,
                    menu_type=MENU_TYPE_BUTTON,
                    parent_id=new_menu.menu_id,
                    order=0,
                    status=STATUS_ENABLED,
                )
                new_buttons.append(button_menu)

            if new_buttons:
                db.add_all(new_buttons)

        return new_menu

    async def update_menu(
        self, db: AsyncSession, menu_id: int, menu_in: MenuUpdate
    ) -> Menu:
        """
        更新菜单信息

        Args:
            db: 数据库会话
            menu_id: 菜单ID
            menu_in: 菜单更新数据

        Returns:
            更新后的菜单对象

        Raises:
            MenuNotFoundException: 菜单不存在
        """
        menu = await db.get(Menu, menu_id)
        if not menu:
            raise MenuNotFoundException()

        update_data = menu_in.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(menu, field, value)

        # 更新按钮权限
        if menu_in.buttons is not None:
            # 删除该菜单下所有现有按钮（只删 button 类型）
            delete_stmt = delete(Menu).where(
                Menu.parent_id == menu_id, Menu.menu_type == MENU_TYPE_BUTTON
            )
            await db.execute(delete_stmt)

            # 批量添加新按钮
            new_buttons = []
            for btn in menu_in.buttons:
                button_menu = Menu(
                    menu_name=btn.desc,
                    permission=btn.code,
                    menu_type=MENU_TYPE_BUTTON,
                    parent_id=menu_id,
                    order=0,
                    status=STATUS_ENABLED,
                )
                new_buttons.append(button_menu)

            if new_buttons:
                db.add_all(new_buttons)

        return menu

    async def delete_menu(self, db: AsyncSession, menu_id: int) -> None:
        """
        删除菜单

        Args:
            db: 数据库会话
            menu_id: 菜单ID

        Raises:
            MenuNotFoundException: 菜单不存在
            BusinessRuleException: 存在子菜单，不能删除
        """
        # 检查是否有子菜单
        child_stmt = select(Menu).where(Menu.parent_id == menu_id)
        child = (await db.execute(child_stmt)).first()
        if child:
            raise BusinessRuleException("请先删除子菜单")

        menu = await db.get(Menu, menu_id)
        if not menu:
            raise MenuNotFoundException()

        await db.delete(menu)

    async def batch_delete_menus(self, db: AsyncSession, ids: list[int]) -> int:
        """
        批量删除菜单

        Args:
            db: 数据库会话
            ids: 菜单ID列表

        Returns:
            删除的菜单数量

        Raises:
            InvalidParameterException: 未选择要删除的菜单
            BusinessRuleException: 存在未选中的子菜单
        """
        if not ids:
            raise InvalidParameterException("请选择要删除的菜单")

        # 批量检查子菜单逻辑 (简单处理：如果选中的菜单中有任何一个包含不在选中列表里的子菜单，则禁止)
        check_stmt = select(Menu).where(
            and_(Menu.parent_id.in_(ids), ~Menu.menu_id.in_(ids))
        )
        has_child = (await db.execute(check_stmt)).first()
        if has_child:
            raise BusinessRuleException("选中的菜单中包含未选中的子菜单，请先处理")

        stmt = delete(Menu).where(Menu.menu_id.in_(ids))
        result = await db.execute(stmt)

        return result.rowcount


# 创建单例
menu_service = MenuService()
