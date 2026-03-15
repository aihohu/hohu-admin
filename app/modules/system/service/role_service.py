from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.exceptions import (
    CannotDeleteAdminException,
    DuplicateRoleException,
    InvalidParameterException,
    RoleNotFoundException,
)
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.schemas.role import RoleCreate, RoleQuery, RoleUpdate
from app.utils.pagination import build_filters, paginate


class RoleService:
    """角色业务逻辑服务"""

    async def get_role_list(self, db: AsyncSession, query: RoleQuery):
        """
        获取角色分页列表

        Args:
            db: 数据库会话
            query: 查询参数

        Returns:
            分页数据对象
        """
        # 构建查询条件
        field_mapping = {
            "role_name": ("role_name", "contains"),
            "role_code": ("role_code", "contains"),
            "status": ("status", "=="),
        }
        filters = build_filters(Role, field_mapping, **query.model_dump())

        # 使用通用分页查询
        page_data = await paginate(
            db=db,
            model=Role,
            query_params=query,
            filters=filters,
            order_by=Role.create_time.desc(),
        )

        return page_data

    async def get_all_roles(self, db: AsyncSession) -> list[Role]:
        """
        获取所有启用的角色列表（不分页）

        Args:
            db: 数据库会话

        Returns:
            角色列表
        """
        stmt = (
            select(Role)
            .where(Role.status == STATUS_ENABLED)
            .order_by(Role.create_time.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_role_menus(self, db: AsyncSession, role_id: int) -> list[str]:
        """
        获取角色的菜单ID列表

        Args:
            db: 数据库会话
            role_id: 角色ID

        Returns:
            菜单ID字符串列表
        """
        from app.db.base import role_menus

        # 子查询：找出该角色拥有的菜单中，作为 parent_id 出现过的 ID
        subquery = (
            select(Menu.parent_id)
            .join(role_menus, Menu.menu_id == role_menus.c.menu_id)
            .where(role_menus.c.role_id == role_id)
            .scalar_subquery()
        )

        # 主查询
        stmt = (
            select(Menu.menu_id)
            .outerjoin(role_menus, Menu.menu_id == role_menus.c.menu_id)
            .where(
                and_(
                    role_menus.c.role_id == role_id,
                    Menu.menu_id.not_in(subquery),
                )
            )
            .order_by(Menu.parent_id, Menu.order)
        )

        result = await db.execute(stmt)
        return [str(menu_id) for menu_id in result.scalars().all()]

    async def create_role(self, db: AsyncSession, role_in: RoleCreate) -> Role:
        """
        创建新角色

        Args:
            db: 数据库会话
            role_in: 角色创建数据

        Returns:
            创建的角色对象

        Raises:
            DuplicateRoleException: 角色编码已存在
        """
        # 检查编码唯一性
        check = await db.execute(
            select(Role).where(Role.role_code == role_in.role_code)
        )
        if check.scalars().first():
            raise DuplicateRoleException(role_in.role_code)

        new_role = Role(**role_in.model_dump())
        db.add(new_role)
        return new_role

    async def update_role(
        self, db: AsyncSession, role_id: int, role_in: RoleUpdate
    ) -> Role:
        """
        更新角色信息

        Args:
            db: 数据库会话
            role_id: 角色ID
            role_in: 角色更新数据

        Returns:
            更新后的角色对象

        Raises:
            RoleNotFoundException: 角色不存在
        """
        role = await db.get(Role, role_id)
        if not role:
            raise RoleNotFoundException()

        update_data = role_in.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(role, field, value)

        return role

    async def update_role_menu(
        self, db: AsyncSession, role_id: int, menu_ids: list[int]
    ) -> Role:
        """
        更新角色的菜单权限

        Args:
            db: 数据库会话
            role_id: 角色ID
            menu_ids: 菜单ID列表

        Returns:
            更新后的角色对象

        Raises:
            RoleNotFoundException: 角色不存在
        """
        role = await db.get(Role, role_id)
        if not role:
            raise RoleNotFoundException()

        if menu_ids:
            menu_result = await db.execute(
                select(Menu).where(Menu.menu_id.in_(menu_ids))
            )
            role.menus = menu_result.scalars().all()
        else:
            role.menus = []

        return role

    async def delete_role(self, db: AsyncSession, role_id: int) -> None:
        """
        删除角色

        Args:
            db: 数据库会话
            role_id: 角色ID

        Raises:
            RoleNotFoundException: 角色不存在
        """
        role = await db.get(Role, role_id)
        if not role:
            raise RoleNotFoundException()

        await db.delete(role)

    async def batch_delete_roles(self, db: AsyncSession, ids: list[int]) -> int:
        """
        批量删除角色

        Args:
            db: 数据库会话
            ids: 角色ID列表

        Returns:
            删除的角色数量

        Raises:
            InvalidParameterException: 未选择要删除的角色
            CannotDeleteAdminException: 尝试删除系统管理员角色
        """
        if not ids:
            raise InvalidParameterException("未选择要删除的角色")

        # 过滤掉 超级管理员 权限，防止误删
        check_stmt = select(Role.role_id).where(
            and_(
                Role.role_id.in_(ids),
                Role.role_code == SUPER_ADMIN_ROLE_CODE,
            )
        )
        admin_result = await db.execute(check_stmt)
        if admin_result.scalars().first():
            raise CannotDeleteAdminException("系统管理员角色")

        stmt = delete(Role).where(Role.role_id.in_(ids))
        result = await db.execute(stmt)

        return result.rowcount

    async def get_role_detail(self, db: AsyncSession, role_id: int) -> Role:
        """
        获取角色详情

        Args:
            db: 数据库会话
            role_id: 角色ID

        Returns:
            角色对象

        Raises:
            RoleNotFoundException: 角色不存在
        """
        role = await db.get(Role, role_id)
        if not role:
            raise RoleNotFoundException()
        return role


# 创建单例
role_service = RoleService()
