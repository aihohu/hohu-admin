from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED
from app.core.exceptions import (
    DictDataNotFoundException,
    InvalidDictTypeException,
    InvalidParameterException,
)
from app.modules.system.models.dict_data import DictData
from app.modules.system.models.dict_type import DictType
from app.modules.system.schemas.dict_data import (
    DictDataCreate,
    DictDataQuery,
    DictDataUpdate,
)
from app.utils.pagination import build_filters, paginate


class DictDataService:
    """字典数据业务逻辑服务"""

    async def get_list(self, db: AsyncSession, query: DictDataQuery):
        """
        获取字典数据分页列表

        Args:
            db: 数据库会话
            query: 查询参数

        Returns:
            分页数据对象
        """
        # 构建查询条件
        field_mapping = {
            "dict_label": ("dict_label", "contains"),
            "dict_value": ("dict_value", "contains"),
            "dict_type": ("dict_type", "contains"),
            "status": ("status", "=="),
        }
        filters = build_filters(DictData, field_mapping, **query.model_dump())

        # 使用通用分页查询
        page_data = await paginate(
            db=db,
            model=DictData,
            query_params=query,
            filters=filters,
            order_by=DictData.dict_sort.asc(),
        )

        return page_data

    async def create(self, db: AsyncSession, data_in: DictDataCreate) -> DictData:
        """
        创建字典数据

        Args:
            db: 数据库会话
            data_in: 字典数据创建数据

        Returns:
            创建的字典数据对象

        Raises:
            InvalidDictTypeException: 字典类型不存在
        """
        # 验证字典类型是否存在
        check_stmt = select(DictType).where(DictType.dict_type == data_in.dict_type)
        result = await db.execute(check_stmt)
        if not result.scalars().first():
            raise InvalidDictTypeException(data_in.dict_type)

        new_data = DictData(**data_in.model_dump())
        db.add(new_data)
        return new_data

    async def update(
        self, db: AsyncSession, data_id: int, data_in: DictDataUpdate
    ) -> DictData:
        """
        更新字典数据信息

        Args:
            db: 数据库会话
            data_id: 字典数据ID
            data_in: 字典数据更新数据

        Returns:
            更新后的字典数据对象

        Raises:
            DictDataNotFoundException: 字典数据不存在
        """
        dict_data = await db.get(DictData, data_id)
        if not dict_data:
            raise DictDataNotFoundException()

        # 如果更新了 dict_type，需要验证新类型是否存在
        if data_in.dict_type is not None and data_in.dict_type != dict_data.dict_type:
            check_stmt = select(DictType).where(DictType.dict_type == data_in.dict_type)
            result = await db.execute(check_stmt)
            if not result.scalars().first():
                raise InvalidDictTypeException(data_in.dict_type)

        update_data = data_in.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(dict_data, field, value)

        return dict_data

    async def delete(self, db: AsyncSession, data_id: int) -> None:
        """
        删除字典数据

        Args:
            db: 数据库会话
            data_id: 字典数据ID

        Raises:
            DictDataNotFoundException: 字典数据不存在
        """
        dict_data = await db.get(DictData, data_id)
        if not dict_data:
            raise DictDataNotFoundException()

        await db.delete(dict_data)

    async def batch_delete(self, db: AsyncSession, ids: list[int]) -> int:
        """
        批量删除字典数据

        Args:
            db: 数据库会话
            ids: 字典数据ID列表

        Returns:
            删除的字典数据数量

        Raises:
            InvalidParameterException: 未选择要删除的数据
        """
        if not ids:
            raise InvalidParameterException("未选择要删除的字典数据")

        stmt = delete(DictData).where(DictData.dict_code.in_(ids))
        result = await db.execute(stmt)

        return result.rowcount

    async def get_by_type(self, db: AsyncSession, dict_type: str) -> list[DictData]:
        """
        根据字典类型获取所有字典数据（按排序）

        Args:
            db: 数据库会话
            dict_type: 字典类型

        Returns:
            字典数据列表

        Raises:
            InvalidDictTypeException: 字典类型不存在
        """
        # 验证字典类型是否存在
        check_stmt = select(DictType).where(DictType.dict_type == dict_type)
        result = await db.execute(check_stmt)
        if not result.scalars().first():
            raise InvalidDictTypeException(dict_type)

        # 查询启用的字典数据
        stmt = (
            select(DictData)
            .where(
                and_(
                    DictData.dict_type == dict_type,
                    DictData.status == STATUS_ENABLED,
                )
            )
            .order_by(DictData.dict_sort.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())


# 创建单例
dict_data_service = DictDataService()
