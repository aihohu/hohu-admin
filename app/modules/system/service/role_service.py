from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import DATA_SCOPE_CUSTOM, STATUS_ENABLED, SUPER_ADMIN_ROLE_CODE
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    DuplicateException,
    InvalidParameterException,
    NotFoundException,
)
from app.db.base import role_depts, role_menus, user_roles
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.system.constants import PHASE3_DESTRUCTIVE_PERMISSIONS
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.schemas.role import RoleCreate, RoleQuery, RoleUpdate
from app.modules.system.service.authorization_lock import authorization_lock_service
from app.modules.system.service.grant_authority import grant_authority_service
from app.utils.pagination import build_filters, paginate


class RoleService:
    """角色业务逻辑服务"""

    async def get_role_list(self, db: AsyncSession, query: RoleQuery):
        """获取角色分页列表"""
        field_mapping = {
            "role_name": ("role_name", "contains"),
            "role_code": ("role_code", "contains"),
            "data_scope": ("data_scope", "=="),
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

        role_data = role_in.model_dump(exclude={"dept_ids"})
        new_role = Role(**role_data)

        # 仅 CUSTOM scope 下 dept_ids 才生效；其他 scope 下传 dept_ids 无意义
        if role_in.data_scope == DATA_SCOPE_CUSTOM and role_in.dept_ids:
            new_role.depts = await self._validate_depts_exist(db, role_in.dept_ids)

        db.add(new_role)
        return new_role

    async def update_role(
        self, db: AsyncSession, role_id: int, role_in: RoleUpdate
    ) -> Role:
        """更新角色信息"""
        role = await db.get(Role, role_id)
        if not role:
            raise NotFoundException("角色")

        old_scope_is_custom = role.data_scope == DATA_SCOPE_CUSTOM

        update_data = role_in.model_dump(exclude_unset=True, exclude={"dept_ids"})
        for field, value in update_data.items():
            setattr(role, field, value)

        new_scope_is_custom = role.data_scope == DATA_SCOPE_CUSTOM
        raw_update = role_in.model_dump(exclude_unset=True)
        dept_ids_provided = "dept_ids" in raw_update

        if dept_ids_provided:
            dept_ids = role_in.dept_ids
            if new_scope_is_custom and dept_ids:
                role.depts = await self._validate_depts_exist(db, dept_ids)
            else:
                # 显式清空：scope 非 CUSTOM 或 dept_ids=[]
                role.depts = []
        elif old_scope_is_custom and not new_scope_is_custom:
            # 离开 CUSTOM 但未传 dept_ids：清空残留，避免下次回 CUSTOM 时旧 depts 复活
            role.depts = []

        return role

    async def _validate_depts_exist(
        self, db: AsyncSession, dept_ids: list[int]
    ) -> list[Dept]:
        """校验所有 dept_ids 都存在，否则抛 InvalidParameterException。"""
        result = await db.execute(select(Dept).where(Dept.dept_id.in_(dept_ids)))
        depts = list(result.scalars().all())
        if len(depts) != len(dept_ids):
            missing = sorted(set(dept_ids) - {d.dept_id for d in depts})
            raise InvalidParameterException(
                f"部门 ID 不存在: {','.join(str(d) for d in missing)}",
                error_code="ROLE_DEPT_NOT_FOUND",
            )
        return depts

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

    async def delete_role(
        self,
        db: AsyncSession,
        role_id: int,
        *,
        actor_user_id: int,
    ) -> None:
        """Delete one unreferenced Role through the destructive policy."""
        await self._delete_roles(
            db,
            [role_id],
            actor_user_id=actor_user_id,
            required_permission="system:role:delete",
        )

    async def _delete_role_facts(
        self,
        db: AsyncSession,
        role_ids: tuple[int, ...],
    ) -> dict[str, tuple]:
        roles = tuple(
            (
                await db.execute(
                    select(Role.role_id, Role.role_code)
                    .where(Role.role_id.in_(role_ids))
                    .order_by(Role.role_id)
                )
            ).all()
        )
        if tuple(int(row.role_id) for row in roles) != role_ids:
            raise NotFoundException("角色")
        members = tuple(
            sorted(
                (int(role_id), int(user_id))
                for role_id, user_id in (
                    await db.execute(
                        select(user_roles.c.role_id, user_roles.c.user_id).where(
                            user_roles.c.role_id.in_(role_ids)
                        )
                    )
                ).all()
            )
        )
        depts = tuple(
            sorted(
                (int(role_id), int(dept_id))
                for role_id, dept_id in (
                    await db.execute(
                        select(role_depts.c.role_id, role_depts.c.dept_id).where(
                            role_depts.c.role_id.in_(role_ids)
                        )
                    )
                ).all()
            )
        )
        return {
            "roles": tuple((int(row.role_id), str(row.role_code)) for row in roles),
            "members": members,
            "depts": depts,
        }

    async def batch_delete_roles(
        self,
        db: AsyncSession,
        ids: list[int],
        *,
        actor_user_id: int,
    ) -> int:
        """Atomically delete unreferenced non-protected Roles as super admin."""
        return await self._delete_roles(
            db,
            ids,
            actor_user_id=actor_user_id,
            required_permission="system:role:batch-delete",
        )

    async def _delete_roles(
        self,
        db: AsyncSession,
        ids: list[int],
        *,
        actor_user_id: int,
        required_permission: str,
    ) -> int:
        """Enforce the exact destructive permission before one atomic delete."""
        if required_permission not in PHASE3_DESTRUCTIVE_PERMISSIONS:
            raise RuntimeError("unsupported role destructive permission")
        if not ids:
            raise InvalidParameterException("未选择要删除的角色")
        normalized = tuple(sorted({int(value) for value in ids}))
        authority = await grant_authority_service.build(db, actor_user_id)
        if not authority.super_admin:
            raise AuthorizationException(
                "仅超级管理员可以删除角色",
                error_code="SUPER_ADMIN_REQUIRED",
            )
        if required_permission not in authority.permission_codes:
            raise AuthorizationException(
                "缺少角色删除权限",
                error_code="MISSING_PERMISSION",
            )
        initial = await self._delete_role_facts(db, normalized)
        if any(
            role_code == SUPER_ADMIN_ROLE_CODE
            for _role_id, role_code in initial["roles"]
        ):
            raise BusinessRuleException(
                "不能删除系统管理员角色",
                error_code="ROLE_DELETE_PROTECTED",
            )
        member_ids = {user_id for _role_id, user_id in initial["members"]}
        dept_ids = {dept_id for _role_id, dept_id in initial["depts"]}
        await authorization_lock_service.lock_targets(
            db,
            role_ids={*authority.enabled_role_ids, *normalized},
            dept_ids=dept_ids,
            user_ids={actor_user_id, *member_ids},
        )
        locked = await self._delete_role_facts(db, normalized)
        if locked != initial:
            raise BusinessRuleException(
                "角色删除引用事实已变化",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        if locked["members"]:
            raise BusinessRuleException(
                "角色仍有关联成员",
                error_code="ROLE_DELETE_REFERENCED",
            )
        await db.execute(delete(RoleAiAgent).where(RoleAiAgent.role_id.in_(normalized)))
        await db.execute(delete(role_menus).where(role_menus.c.role_id.in_(normalized)))
        await db.execute(delete(role_depts).where(role_depts.c.role_id.in_(normalized)))
        result = await db.execute(delete(Role).where(Role.role_id.in_(normalized)))
        return int(result.rowcount or 0)

    async def get_role_detail(self, db: AsyncSession, role_id: int) -> Role:
        """获取角色详情（包含 dept_ids）"""
        role = await db.get(Role, role_id)
        if not role:
            raise NotFoundException("角色")
        return role


# 创建单例
role_service = RoleService()
