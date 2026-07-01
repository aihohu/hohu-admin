from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import DATA_SCOPE_CUSTOM, STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.exceptions import (
    BusinessRuleException,
    DuplicateException,
    InvalidParameterException,
    NotFoundException,
)
from app.db.base import role_menus
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.schemas.role import RoleCreate, RoleQuery, RoleUpdate
from app.utils.pagination import build_filters, paginate


class RoleService:
    """角色业务逻辑服务"""

    async def get_role_list(self, db: AsyncSession, query: RoleQuery):
        """获取角色分页列表"""
        field_mapping = {
            "role_name": ("role_name", "contains"),
            "role_code": ("role_code", "contains"),
            "status": ("status", "=="),
        }
        filters = build_filters(Role, field_mapping, **query.model_dump())

        page_data = await paginate(
            db=db,
            model=Role,
            query_params=query,
            filters=filters,
            order_by=Role.create_time.desc(),
        )

        return page_data

    async def get_all_roles(self, db: AsyncSession) -> list[Role]:
        """获取所有启用的角色列表（不分页）"""
        stmt = (
            select(Role)
            .where(Role.status == STATUS_ENABLED)
            .order_by(Role.create_time.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_role_menus(self, db: AsyncSession, role_id: int) -> list[str]:
        """返回角色拥有的「真叶子」菜单 ID（前端 NTree cascade 据此推导父子状态）。

        设计权衡：
        - 只返回叶子（menu 表中没有子的菜单），不返回父。
        - 否则 NaiveUI NTree cascade 会反向级联「父 checked → 所有当前子
          checked」，造成"全选"显示 bug。
        - 用 NOT IN (所有非 NULL 的 parent_id 集合) 排除任何父，已处理
          parent_id=NULL 的根菜单（旧实现的 NOT IN 因 subquery 含 NULL 会
          让整个查询失效）。
        - 已知限制：孤立父（role 拥有 M1，M1 在 menu 表中有子但子未被 role
          拥有）会被排除，前端显示为未勾选。新代码不再产生孤立父（参见
          menu_service.update_menu 按 permission 增量更新），存量孤立父
          需管理员重新配置权限。
        """
        all_parents_sq = select(Menu.parent_id).where(Menu.parent_id.is_not(None))

        stmt = (
            select(Menu.menu_id)
            .join(role_menus, Menu.menu_id == role_menus.c.menu_id)
            .where(
                role_menus.c.role_id == role_id,
                Menu.menu_id.not_in(all_parents_sq),
            )
            .order_by(Menu.parent_id, Menu.order)
        )

        result = await db.execute(stmt)
        return [str(menu_id) for menu_id in result.scalars().all()]

    async def create_role(self, db: AsyncSession, role_in: RoleCreate) -> Role:
        """创建新角色"""
        check = await db.execute(
            select(Role).where(Role.role_code == role_in.role_code)
        )
        if check.scalars().first():
            raise DuplicateException("角色编码", role_in.role_code)

        dept_ids = role_in.dept_ids
        role_data = role_in.model_dump(exclude={"dept_ids"})
        new_role = Role(**role_data)

        # 处理自定义数据权限的部门关联
        if dept_ids and role_in.data_scope == DATA_SCOPE_CUSTOM:
            dept_result = await db.execute(
                select(Dept).where(Dept.dept_id.in_(dept_ids))
            )
            new_role.depts = list(dept_result.scalars().all())

        db.add(new_role)
        return new_role

    async def update_role(
        self, db: AsyncSession, role_id: int, role_in: RoleUpdate
    ) -> Role:
        """更新角色信息"""
        role = await db.get(Role, role_id)
        if not role:
            raise NotFoundException("角色")

        update_data = role_in.model_dump(exclude_unset=True, exclude={"dept_ids"})
        for field, value in update_data.items():
            setattr(role, field, value)

        # 处理部门关联
        if "dept_ids" in role_in.model_dump(exclude_unset=True):
            dept_ids = role_in.dept_ids
            if dept_ids and role.data_scope == DATA_SCOPE_CUSTOM:
                dept_result = await db.execute(
                    select(Dept).where(Dept.dept_id.in_(dept_ids))
                )
                role.depts = list(dept_result.scalars().all())
            else:
                role.depts = []

        return role

    async def update_role_menu(
        self, db: AsyncSession, role_id: int, menu_ids: list[int]
    ) -> Role:
        """更新角色的菜单权限"""
        role = await db.get(Role, role_id)
        if not role:
            raise NotFoundException("角色")

        if menu_ids:
            menu_result = await db.execute(
                select(Menu).where(Menu.menu_id.in_(menu_ids))
            )
            role.menus = menu_result.scalars().all()
        else:
            role.menus = []

        return role

    async def delete_role(self, db: AsyncSession, role_id: int) -> None:
        """删除角色"""
        role = await db.get(Role, role_id)
        if not role:
            raise NotFoundException("角色")

        await db.delete(role)

    async def batch_delete_roles(self, db: AsyncSession, ids: list[int]) -> int:
        """批量删除角色"""
        if not ids:
            raise InvalidParameterException("未选择要删除的角色")

        check_stmt = select(Role.role_id).where(
            and_(
                Role.role_id.in_(ids),
                Role.role_code == SUPER_ADMIN_ROLE_CODE,
            )
        )
        admin_result = await db.execute(check_stmt)
        if admin_result.scalars().first():
            raise BusinessRuleException("不能删除系统管理员角色")

        stmt = delete(Role).where(Role.role_id.in_(ids))
        result = await db.execute(stmt)

        return result.rowcount

    async def get_role_detail(self, db: AsyncSession, role_id: int) -> Role:
        """获取角色详情（包含 dept_ids）"""
        role = await db.get(Role, role_id)
        if not role:
            raise NotFoundException("角色")
        return role


# 创建单例
role_service = RoleService()
