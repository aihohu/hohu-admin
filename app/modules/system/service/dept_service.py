from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DEPT_MAX_LEVEL,
    IS_PRIMARY_NO,
    IS_PRIMARY_YES,
    STATUS_ENABLED,
)
from app.core.exceptions import (
    BusinessRuleException,
    DuplicateException,
    InvalidParameterException,
    NotFoundException,
)
from app.db.base import user_depts
from app.modules.system.models.dept import Dept
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import (
    DeptCreate,
    DeptQuery,
    DeptUpdate,
    DeptUserItem,
    DeptUsersOut,
)
from app.modules.system.service.config_service import config_service
from app.utils.pagination import build_filters, paginate


class DeptService:
    """部门业务逻辑服务"""

    async def get_list(self, db: AsyncSession, query: DeptQuery):
        """获取分页列表"""
        field_mapping = {
            "dept_name": ("dept_name", "contains"),
            "status": ("status", "=="),
            "leader": ("leader", "contains"),
        }
        filters = build_filters(Dept, field_mapping, **query.model_dump())
        return await paginate(
            db=db,
            model=Dept,
            query_params=query,
            filters=filters,
            order_by=Dept.order_num.asc(),
        )

    async def get_all(self, db: AsyncSession) -> list[Dept]:
        """获取全量列表（不分页），用于构建树"""
        stmt = select(Dept).order_by(Dept.order_num.asc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, db: AsyncSession, dept_id: int) -> Dept:
        """根据 ID 获取部门"""
        dept = await db.get(Dept, dept_id)
        if not dept:
            raise NotFoundException("部门")
        return dept

    async def get_by_ids(self, db: AsyncSession, ids: list[int]) -> list[Dept]:
        """批量查询部门"""
        if not ids:
            return []
        result = await db.execute(select(Dept).where(Dept.dept_id.in_(ids)))
        return list(result.scalars().all())

    async def create(self, db: AsyncSession, dept_in: DeptCreate) -> Dept:
        """创建部门"""
        # parent_id 为 None 或 0 都视为顶级部门
        parent_id = dept_in.parent_id if dept_in.parent_id else None

        parent = None
        if parent_id is not None:
            parent = await db.get(Dept, parent_id)
            if not parent:
                raise NotFoundException("部门")

            # 校验层级深度
            level = self._get_dept_level(parent.ancestors) + 1
            if level > DEPT_MAX_LEVEL:
                raise BusinessRuleException(f"部门层级不能超过{DEPT_MAX_LEVEL}层")

        # 校验同级名称唯一性
        await self._check_duplicate_name(db, parent_id, dept_in.dept_name)

        # 计算 ancestors
        if parent:
            ancestors = f"{parent.ancestors},{parent.dept_id}"
        else:
            ancestors = "0"

        new_dept = Dept(
            parent_id=parent_id,
            ancestors=ancestors,
            dept_name=dept_in.dept_name,
            order_num=dept_in.order_num,
            leader=dept_in.leader,
            phone=dept_in.phone,
            email=dept_in.email if dept_in.email else None,
            status=dept_in.status,
        )
        db.add(new_dept)
        return new_dept

    async def update(self, db: AsyncSession, dept_id: int, dept_in: DeptUpdate) -> Dept:
        """更新部门"""
        dept = await self.get_by_id(db, dept_id)
        update_data = dept_in.model_dump(exclude_unset=True)

        # 处理 parent_id 变更（0 视为顶级）
        new_parent_id = update_data.get("parent_id")
        normalized_new = new_parent_id if new_parent_id else None
        if "parent_id" in update_data and normalized_new != dept.parent_id:
            # 不能将自己设为父级
            if new_parent_id == dept_id:
                raise BusinessRuleException("不能将自己设为父级部门")

            if new_parent_id is not None:
                new_parent = await db.get(Dept, new_parent_id)
                if not new_parent:
                    raise NotFoundException("部门")

                # 不能移动到自己的后代下
                if new_parent.ancestors and str(dept_id) in new_parent.ancestors.split(
                    ","
                ):
                    raise BusinessRuleException("不能将部门移动到自己的后代下")

                # 校验新层级深度
                child_depth = await self._get_max_child_depth(db, dept_id)
                new_level = self._get_dept_level(new_parent.ancestors) + 1 + child_depth
                if new_level > DEPT_MAX_LEVEL:
                    raise BusinessRuleException(f"部门层级不能超过{DEPT_MAX_LEVEL}层")

                # 更新后代 ancestors
                old_prefix = dept.ancestors
                new_prefix = f"{new_parent.ancestors},{new_parent.dept_id}"
                await self._update_descendants_ancestors(
                    db, dept_id, old_prefix, new_prefix
                )

                dept.ancestors = new_prefix
            else:
                # 移动到顶级
                old_prefix = dept.ancestors
                new_prefix = "0"
                await self._update_descendants_ancestors(
                    db, dept_id, old_prefix, new_prefix
                )
                dept.ancestors = new_prefix

        # 处理名称变更
        new_name = update_data.get("dept_name")
        if new_name and new_name != dept.dept_name:
            parent_id_to_check = update_data.get("parent_id", dept.parent_id)
            await self._check_duplicate_name(
                db, parent_id_to_check, new_name, exclude_id=dept_id
            )

        # 更新字段
        for field, value in update_data.items():
            if field != "parent_id":  # parent_id 已在上面处理
                setattr(dept, field, value)

        return dept

    async def delete(self, db: AsyncSession, dept_id: int) -> None:
        """删除部门"""
        dept = await self.get_by_id(db, dept_id)

        # 检查子部门
        child_stmt = select(Dept).where(Dept.parent_id == dept_id)
        child = (await db.execute(child_stmt)).first()
        if child:
            raise BusinessRuleException("请先删除部门的子节点")

        # 检查关联用户
        user_stmt = select(user_depts).where(user_depts.c.dept_id == dept_id)
        user_assoc = (await db.execute(user_stmt)).first()
        if user_assoc:
            raise BusinessRuleException("该部门下存在用户，请先转移用户")

        await db.delete(dept)

    async def batch_delete(self, db: AsyncSession, ids: list[int]) -> int:
        """批量删除部门"""
        if not ids:
            raise InvalidParameterException("请选择要删除的部门")

        # 检查未选中的子部门
        check_stmt = select(Dept).where(
            and_(Dept.parent_id.in_(ids), ~Dept.dept_id.in_(ids))
        )
        has_child = (await db.execute(check_stmt)).first()
        if has_child:
            raise BusinessRuleException("选中的部门中包含未选中的子部门")

        # 检查关联用户
        user_stmt = select(user_depts).where(user_depts.c.dept_id.in_(ids))
        user_assoc = (await db.execute(user_stmt)).first()
        if user_assoc:
            raise BusinessRuleException("选中的部门中存在关联用户")

        stmt = delete(Dept).where(Dept.dept_id.in_(ids))
        result = await db.execute(stmt)
        return result.rowcount

    async def update_user_depts(
        self, db: AsyncSession, user_id: int, dept_list: list[dict]
    ) -> None:
        """更新用户的部门关联（空列表表示清空所有关联）"""
        # 先删除旧关联，使空列表也能正确清空
        await db.execute(delete(user_depts).where(user_depts.c.user_id == user_id))

        if not dept_list:
            return

        # 校验主部门
        primary_count = sum(1 for d in dept_list if d.get("is_primary"))
        if primary_count == 0:
            raise BusinessRuleException("必须指定一个主部门")
        if primary_count > 1:
            raise BusinessRuleException("只能指定一个主部门")

        # 校验部门都存在
        dept_ids = [int(d["dept_id"]) for d in dept_list]
        depts = await self.get_by_ids(db, dept_ids)
        if len(depts) != len(dept_ids):
            raise NotFoundException("部门")

        # 插入新关联
        for dept_item in dept_list:
            await db.execute(
                user_depts.insert().values(
                    user_id=user_id,
                    dept_id=int(dept_item["dept_id"]),
                    is_primary=IS_PRIMARY_YES
                    if dept_item.get("is_primary")
                    else IS_PRIMARY_NO,
                )
            )

    async def get_dept_users(self, db: AsyncSession, dept_id: int) -> DeptUsersOut:
        """获取部门用户管理数据：所有启用用户 + 是否在该部门的标记"""
        dept = await self.get_by_id(db, dept_id)

        # 查询所有启用用户
        users_result = await db.execute(
            select(User)
            .where(User.status == STATUS_ENABLED)
            .order_by(User.create_time.desc())
        )
        users = list(users_result.scalars().all())

        # 查询该部门的用户关联
        member_result = await db.execute(
            select(user_depts.c.user_id, user_depts.c.is_primary).where(
                user_depts.c.dept_id == dept_id
            )
        )
        member_map: dict[int, bool] = {
            uid: (is_primary == IS_PRIMARY_YES)
            for uid, is_primary in member_result.all()
        }

        items = [
            DeptUserItem(
                user_id=u.user_id,
                user_name=u.user_name,
                nickname=u.nickname,
                user_email=u.user_email,
                user_phone=u.user_phone,
                status=u.status,
                is_member=u.user_id in member_map,
                is_primary=member_map.get(u.user_id, False),
            )
            for u in users
        ]

        return DeptUsersOut(dept_id=dept.dept_id, dept_name=dept.dept_name, users=items)

    async def update_dept_users(
        self, db: AsyncSession, dept_id: int, user_ids: list[int]
    ) -> dict:
        """批量更新部门用户关联：传入最终成员列表，diff 出新增/移除"""
        await self.get_by_id(db, dept_id)

        # 查询当前成员及主部门标记
        current_result = await db.execute(
            select(user_depts.c.user_id, user_depts.c.is_primary).where(
                user_depts.c.dept_id == dept_id
            )
        )
        current_map: dict[int, bool] = {
            uid: (is_primary == IS_PRIMARY_YES)
            for uid, is_primary in current_result.all()
        }
        current_ids = set(current_map.keys())
        target_ids = set(user_ids)

        to_add = target_ids - current_ids
        to_remove = current_ids - target_ids

        # 系统策略校验：若强制主部门，禁止移除导致用户无部门的操作
        if to_remove:
            if await config_service.get_bool(db, "user_require_primary_dept"):
                # 检查每个待移除用户在本部门之外是否还有部门
                other_result = await db.execute(
                    select(user_depts.c.user_id).where(
                        and_(
                            user_depts.c.user_id.in_(to_remove),
                            user_depts.c.dept_id != dept_id,
                        )
                    )
                )
                users_with_other = set(other_result.scalars().all())
                orphan_users = to_remove - users_with_other
                if orphan_users:
                    raise BusinessRuleException(
                        f"系统已开启「强制用户主部门」，移除后将导致 {len(orphan_users)} 名用户无任何部门",
                        error_code="USER_PRIMARY_DEPT_REQUIRED",
                    )

        # 新增关联（默认非主部门）
        if to_add:
            await db.execute(
                user_depts.insert(),
                [
                    {
                        "user_id": uid,
                        "dept_id": dept_id,
                        "is_primary": IS_PRIMARY_NO,
                    }
                    for uid in to_add
                ],
            )

        # 移除关联；若是主部门，需为受影响用户重新指定主部门
        if to_remove:
            await db.execute(
                delete(user_depts).where(
                    and_(
                        user_depts.c.dept_id == dept_id,
                        user_depts.c.user_id.in_(to_remove),
                    )
                )
            )

            removed_primary_users = [uid for uid in to_remove if current_map.get(uid)]
            for uid in removed_primary_users:
                # 查询该用户剩余的任意一个部门，设为主部门
                other_result = await db.execute(
                    select(user_depts.c.dept_id)
                    .where(user_depts.c.user_id == uid)
                    .order_by(user_depts.c.dept_id.asc())
                    .limit(1)
                )
                other_dept = other_result.scalars().first()
                if other_dept is not None:
                    await db.execute(
                        user_depts.update()
                        .where(
                            and_(
                                user_depts.c.user_id == uid,
                                user_depts.c.dept_id == other_dept,
                            )
                        )
                        .values(is_primary=IS_PRIMARY_YES)
                    )

        return {"added": len(to_add), "removed": len(to_remove)}

    def _get_dept_level(self, ancestors: str | None) -> int:
        """根据 ancestors 计算当前层级"""
        if not ancestors:
            return 1
        return len(ancestors.split(","))

    async def _get_max_child_depth(self, db: AsyncSession, dept_id: int) -> int:
        """获取子树最大深度（相对于当前节点）"""
        dept = await db.get(Dept, dept_id)
        if not dept:
            return 0

        # 查询所有后代
        ancestor_prefix = f"{dept.ancestors},{dept_id}"
        stmt = select(Dept).where(Dept.ancestors.like(f"{ancestor_prefix}%"))
        result = await db.execute(stmt)
        descendants = result.scalars().all()

        if not descendants:
            return 0

        # 计算最大深度
        max_depth = 0
        base_level = len(ancestor_prefix.split(","))
        for desc in descendants:
            level = len(desc.ancestors.split(","))
            depth = level - base_level
            if depth > max_depth:
                max_depth = depth

        return max_depth

    async def _update_descendants_ancestors(
        self, db: AsyncSession, dept_id: int, old_prefix: str, new_prefix: str
    ) -> None:
        """移动部门时更新后代 ancestors"""
        ancestor_pattern = f"{old_prefix},{dept_id}"
        stmt = select(Dept).where(Dept.ancestors.like(f"{ancestor_pattern}%"))
        result = await db.execute(stmt)
        descendants = result.scalars().all()

        for desc in descendants:
            desc.ancestors = desc.ancestors.replace(old_prefix, new_prefix, 1)

    async def _check_duplicate_name(
        self,
        db: AsyncSession,
        parent_id: int | None,
        dept_name: str,
        exclude_id: int | None = None,
    ) -> None:
        """校验同级名称唯一性"""
        stmt = select(Dept).where(Dept.dept_name == dept_name)
        if parent_id is not None:
            stmt = stmt.where(Dept.parent_id == parent_id)
        else:
            stmt = stmt.where(Dept.parent_id.is_(None))

        if exclude_id:
            stmt = stmt.where(Dept.dept_id != exclude_id)

        result = await db.execute(stmt)
        if result.scalars().first():
            raise DuplicateException("同级部门名称", dept_name)


# 创建单例
dept_service = DeptService()
