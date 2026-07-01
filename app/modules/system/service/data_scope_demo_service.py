"""数据权限演示业务表 service。

list 端点真应用 get_data_scope_filters，让不同 data_scope 的用户看到不同
数据子集，是整个演示功能的核心展示位。

create 时 dept_id 从 current_user 的主部门取，create_by 取 user_id，
schema 不接受前端传这两个字段，防止伪造绕过权限语义。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException, NotFoundException
from app.db.base import user_depts
from app.modules.system.models.data_scope_demo import DataScopeDemo
from app.modules.system.models.user import User
from app.modules.system.schemas.data_scope_demo import (
    DataScopeDemoCreate,
    DataScopeDemoQuery,
    DataScopeDemoUpdate,
)
from app.utils.data_scope import get_data_scope_filters
from app.utils.pagination import build_filters, paginate


class DataScopeDemoService:
    """数据权限演示业务服务"""

    async def get_list(
        self, db: AsyncSession, query: DataScopeDemoQuery, current_user: User
    ):
        """获取分页列表（含数据权限过滤）。

        关键：调用 get_data_scope_filters 注入过滤条件。这是演示的核心
        展示位——同一份数据，不同 current_user 看到不同子集。
        """
        field_mapping = {
            "title": ("title", "contains"),
            "status": ("status", "=="),
        }
        filters = build_filters(DataScopeDemo, field_mapping, **query.model_dump())

        scope_filters = await get_data_scope_filters(
            db,
            current_user,
            DataScopeDemo,
            dept_field="dept_id",
            user_field="create_by",
        )
        filters.extend(scope_filters)

        return await paginate(
            db=db,
            model=DataScopeDemo,
            query_params=query,
            filters=filters,
            order_by=DataScopeDemo.create_time.desc(),
        )

    async def create(
        self,
        db: AsyncSession,
        data_in: DataScopeDemoCreate,
        current_user: User,
    ) -> DataScopeDemo:
        """创建演示数据。

        dept_id 从 current_user 主部门取（user_depts.is_primary='Y'），
        create_by 取 current_user.user_id。前端无法在 schema 中传这两个字段，
        service 强制注入，防止伪造。
        """
        primary_dept_id = await self._get_primary_dept_id(db, current_user.user_id)
        if primary_dept_id is None:
            raise BusinessRuleException(
                "当前用户未配置主部门，无法创建数据",
                error_code="USER_NO_PRIMARY_DEPT",
            )

        demo = DataScopeDemo(
            title=data_in.title,
            content=data_in.content,
            status=data_in.status,
            dept_id=primary_dept_id,
            create_by=current_user.user_id,
        )
        db.add(demo)
        return demo

    async def update(
        self, db: AsyncSession, demo_id: int, data_in: DataScopeDemoUpdate
    ) -> DataScopeDemo:
        """更新演示数据（不允许改 dept_id/create_by）。"""
        demo = await db.get(DataScopeDemo, demo_id)
        if not demo:
            raise NotFoundException("演示数据")

        update_data = data_in.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(demo, field, value)
        return demo

    async def delete(self, db: AsyncSession, demo_id: int) -> None:
        """删除演示数据"""
        demo = await db.get(DataScopeDemo, demo_id)
        if not demo:
            raise NotFoundException("演示数据")
        await db.delete(demo)

    async def _get_primary_dept_id(self, db: AsyncSession, user_id: int) -> int | None:
        """取用户主部门 ID（user_depts.is_primary='Y'）。无主部门返回 None。"""
        stmt = select(user_depts.c.dept_id).where(
            user_depts.c.user_id == user_id,
            user_depts.c.is_primary == "Y",
        )
        result = await db.execute(stmt)
        return result.scalars().first()


# 创建单例
data_scope_demo_service = DataScopeDemoService()
