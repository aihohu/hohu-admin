from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import DEPT_MAX_LEVEL, IS_PRIMARY_NO, IS_PRIMARY_YES
from app.core.exceptions import (
    BusinessRuleException,
    DeptLevelExceededException,
    DeptNotFoundException,
    DuplicateDeptNameException,
    HasChildrenException,
    HasUsersException,
    InvalidParameterException,
    MultiplePrimaryDeptException,
    PrimaryDeptRequiredException,
)
from app.db.base import user_depts
from app.modules.system.models.dept import Dept
from app.modules.system.schemas.dept import DeptCreate, DeptQuery, DeptUpdate
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
            raise DeptNotFoundException()
        return dept

    async def get_by_ids(self, db: AsyncSession, ids: list[int]) -> list[Dept]:
        """批量查询部门"""
        if not ids:
            return []
        result = await db.execute(select(Dept).where(Dept.dept_id.in_(ids)))
        return list(result.scalars().all())

    async def create(self, db: AsyncSession, dept_in: DeptCreate) -> Dept:
        """创建部门"""
        parent = None
        if dept_in.parent_id is not None:
            parent = await db.get(Dept, dept_in.parent_id)
            if not parent:
                raise DeptNotFoundException()

            # 校验层级深度
            level = self._get_dept_level(parent.ancestors) + 1
            if level > DEPT_MAX_LEVEL:
                raise DeptLevelExceededException(DEPT_MAX_LEVEL)

        # 校验同级名称唯一性
        await self._check_duplicate_name(db, dept_in.parent_id, dept_in.dept_name)

        # 计算 ancestors
        if parent:
            ancestors = f"{parent.ancestors},{parent.dept_id}"
        else:
            ancestors = "0"

        new_dept = Dept(
            parent_id=dept_in.parent_id,
            ancestors=ancestors,
            dept_name=dept_in.dept_name,
            order_num=dept_in.order_num,
            leader=dept_in.leader,
            phone=dept_in.phone,
            email=dept_in.email,
            status=dept_in.status,
        )
        db.add(new_dept)
        return new_dept

    async def update(self, db: AsyncSession, dept_id: int, dept_in: DeptUpdate) -> Dept:
        """更新部门"""
        dept = await self.get_by_id(db, dept_id)
        update_data = dept_in.model_dump(exclude_unset=True)

        # 处理 parent_id 变更
        new_parent_id = update_data.get("parent_id")
        if "parent_id" in update_data and new_parent_id != dept.parent_id:
            # 不能将自己设为父级
            if new_parent_id == dept_id:
                raise BusinessRuleException("不能将自己设为父级部门")

            if new_parent_id is not None:
                new_parent = await db.get(Dept, new_parent_id)
                if not new_parent:
                    raise DeptNotFoundException()

                # 不能移动到自己的后代下
                if new_parent.ancestors and str(dept_id) in new_parent.ancestors.split(
                    ","
                ):
                    raise BusinessRuleException("不能将部门移动到自己的后代下")

                # 校验新层级深度
                child_depth = await self._get_max_child_depth(db, dept_id)
                new_level = self._get_dept_level(new_parent.ancestors) + 1 + child_depth
                if new_level > DEPT_MAX_LEVEL:
                    raise DeptLevelExceededException(DEPT_MAX_LEVEL)

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
            raise HasChildrenException("部门")

        # 检查关联用户
        user_stmt = select(user_depts).where(user_depts.c.dept_id == dept_id)
        user_assoc = (await db.execute(user_stmt)).first()
        if user_assoc:
            raise HasUsersException()

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
        """更新用户的部门关联"""
        if not dept_list:
            return

        # 校验主部门
        primary_count = sum(1 for d in dept_list if d.get("is_primary"))
        if primary_count == 0:
            raise PrimaryDeptRequiredException()
        if primary_count > 1:
            raise MultiplePrimaryDeptException()

        # 校验部门都存在
        dept_ids = [int(d["dept_id"]) for d in dept_list]
        depts = await self.get_by_ids(db, dept_ids)
        if len(depts) != len(dept_ids):
            raise DeptNotFoundException()

        # 删除旧关联
        await db.execute(delete(user_depts).where(user_depts.c.user_id == user_id))

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
            raise DuplicateDeptNameException(dept_name)


# 创建单例
dept_service = DeptService()
