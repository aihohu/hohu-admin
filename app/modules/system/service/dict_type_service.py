from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED
from app.core.exceptions import (
    BusinessRuleException,
    DuplicateException,
    NotFoundException,
)
from app.modules.system.models.dict_data import DictData
from app.modules.system.models.dict_type import DictType
from app.modules.system.schemas.dict_type import (
    DictTypeCreate,
    DictTypeQuery,
    DictTypeUpdate,
)
from app.utils.pagination import build_filters, paginate


class DictTypeService:
    """字典类型业务逻辑服务"""

    async def get_list(self, db: AsyncSession, query: DictTypeQuery):
        """
        获取字典类型分页列表

        Args:
            db: 数据库会话
            query: 查询参数

        Returns:
            分页数据对象
        """
        # 构建查询条件
        field_mapping = {
            "dict_name": ("dict_name", "contains"),
            "dict_type": ("dict_type", "contains"),
            "status": ("status", "=="),
        }
        filters = build_filters(DictType, field_mapping, **query.model_dump())

        # 使用通用分页查询
        page_data = await paginate(
            db=db,
            model=DictType,
            query_params=query,
            filters=filters,
            order_by=DictType.create_time.desc(),
        )

        return page_data

    async def create(self, db: AsyncSession, type_in: DictTypeCreate) -> DictType:
        """
        创建字典类型

        Args:
            db: 数据库会话
            type_in: 字典类型创建数据

        Returns:
            创建的字典类型对象

        Raises:
            DuplicateException: 字典类型已存在
        """
        # 检查编码唯一性
        check = await db.execute(
            select(DictType).where(DictType.dict_type == type_in.dict_type)
        )
        if check.scalars().first():
            raise DuplicateException("字典类型", type_in.dict_type)

        # 检查名称唯一性
        check_name = await db.execute(
            select(DictType).where(DictType.dict_name == type_in.dict_name)
        )
        if check_name.scalars().first():
            raise DuplicateException("字典类型", type_in.dict_name)

        new_type = DictType(**type_in.model_dump())
        db.add(new_type)
        return new_type

    async def update(
        self, db: AsyncSession, type_id: int, type_in: DictTypeUpdate
    ) -> DictType:
        """
        更新字典类型信息

        Args:
            db: 数据库会话
            type_id: 字典类型ID
            type_in: 字典类型更新数据

        Returns:
            更新后的字典类型对象

        Raises:
            NotFoundException: 字典类型不存在
        """
        dict_type = await db.get(DictType, type_id)
        if not dict_type:
            raise NotFoundException("字典类型")

        update_data = type_in.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(dict_type, field, value)

        return dict_type

    async def delete(self, db: AsyncSession, type_id: int) -> None:
        """
        删除字典类型

        Args:
            db: 数据库会话
            type_id: 字典类型ID

        Raises:
            NotFoundException: 字典类型不存在
            BusinessRuleException: 字典类型下有数据
        """
        dict_type = await db.get(DictType, type_id)
        if not dict_type:
            raise NotFoundException("字典类型")

        # 检查是否有关联的字典数据
        check_stmt = select(DictData).where(
            and_(DictData.dict_type == dict_type.dict_type)
        )
        result = await db.execute(check_stmt)
        if result.scalars().first():
            raise BusinessRuleException("该字典类型下存在数据，请先删除数据")

        await db.delete(dict_type)

    async def get_all_enabled(self, db: AsyncSession) -> list[DictType]:
        """
        获取所有启用的字典类型列表（不分页）

        Args:
            db: 数据库会话

        Returns:
            字典类型列表
        """
        stmt = (
            select(DictType)
            .where(DictType.status == STATUS_ENABLED)
            .order_by(DictType.create_time.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def batch_delete(self, db: AsyncSession, ids: list[int]) -> int:
        """
        批量删除字典类型

        Args:
            db: 数据库会话
            ids: 字典类型ID列表

        Returns:
            删除的数量

        Raises:
            BusinessRuleException: 有字典类型下存在数据
        """
        # 检查是否有字典类型下存在数据
        dict_types = await db.execute(
            select(DictType).where(DictType.dict_type_id.in_(ids))
        )
        dict_type_list = dict_types.scalars().all()

        for dict_type in dict_type_list:
            check_stmt = select(DictData).where(
                DictData.dict_type == dict_type.dict_type
            )
            result = await db.execute(check_stmt)
            if result.scalars().first():
                raise BusinessRuleException("该字典类型下存在数据，请先删除数据")

        # 删除所有字典类型
        for dict_type in dict_type_list:
            await db.delete(dict_type)

        return len(dict_type_list)


# 创建单例
dict_type_service = DictTypeService()
