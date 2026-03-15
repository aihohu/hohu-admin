from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.core.base_response import PageResult


class QueryParams(BaseModel):
    """基础查询参数"""

    current: int = 1
    size: int = 10


async def paginate(
    db: AsyncSession,
    model: Any,
    query_params: QueryParams,
    filters: list[Any] | None = None,
    order_by: Any = None,
    eager_loads: list[Any] | None = None,
) -> PageResult:
    """
    通用分页查询函数（适用于简单查询）

    Args:
        db: 数据库会话
        model: SQLAlchemy 模型类
        query_params: 查询参数（包含 current, size）
        filters: 查询条件列表
        order_by: 排序条件
        eager_loads: 预加载的关联关系列表

    Returns:
        PageResult: 分页结果
    """
    # 构建查询条件
    query = select(model)

    # 添加预加载
    if eager_loads:
        for eager_load in eager_loads:
            query = query.options(eager_load)

    # 添加过滤条件
    if filters:
        query = query.where(and_(*filters))

    # 查询总数
    count_stmt = select(func.count()).select_from(model)
    if filters:
        count_stmt = count_stmt.where(and_(*filters))
    total = (await db.execute(count_stmt)).scalar() or 0

    # 添加排序
    # 注意：SQLAlchemy 表达式对象不能直接用于布尔判断，必须使用 is not None
    if order_by is not None:
        query = query.order_by(order_by)

    # 分页查询数据
    offset = (query_params.current - 1) * query_params.size
    query = query.offset(offset).limit(query_params.size)

    result = await db.execute(query)
    records = result.scalars().all()

    return PageResult(
        records=records,
        total=total,
        current=query_params.current,
        size=query_params.size,
    )


async def paginate_custom(
    db: AsyncSession,
    query: Select,
    count_query: Select | None = None,
    current: int = 1,
    size: int = 10,
) -> PageResult:
    """
    自定义分页查询函数（适用于复杂 SQL 查询）

    Args:
        db: 数据库会话
        query: 已构建的 SQLAlchemy 查询对象（包含所有条件、JOIN等）
        count_query: 可选的自定义计数查询，如果不提供则尝试从 query 推断
        current: 当前页码
        size: 每页大小

    Returns:
        PageResult: 分页结果

    示例:
        # 复杂 JOIN 查询
        stmt = (
            select(User, Role.role_name)
            .join(Role, User.roles)
            .where(and_(...))
        )
        page_data = await paginate_custom(db, stmt, current=1, size=10)
    """
    # 1. 执行计数查询
    if count_query is not None:
        total = (await db.execute(count_query)).scalar() or 0
    else:
        # 尝试从主查询推断计数
        # 对于复杂查询，建议显式提供 count_query
        try:
            # 创建子查询别名进行计数
            subquery = query.alias("subquery")
            count_query = select(func.count()).select_from(subquery)
            total = (await db.execute(count_query)).scalar() or 0
        except Exception:
            # 如果自动推断失败，默认返回 0
            total = 0

    # 2. 分页查询数据
    offset = (current - 1) * size
    paginated_query = query.offset(offset).limit(size)

    result = await db.execute(paginated_query)
    records = result.all()  # 使用 all() 而不是 scalars().all()，支持多列查询

    return PageResult(
        records=records,
        total=total,
        current=current,
        size=size,
    )


def build_filters(
    model: Any,
    field_mapping: dict[str, str],
    **kwargs,
) -> list[Any]:
    """
    根据字段映射构建查询条件

    Args:
        model: SQLAlchemy 模型类
        field_mapping: 字段映射字典 {参数名: 模型字段名或操作}
        **kwargs: 查询参数

    Returns:
        查询条件列表
    """
    filters = []
    for param_name, value in kwargs.items():
        if value is None or value == "":
            continue

        if param_name not in field_mapping:
            continue

        field_info = field_mapping[param_name]

        if isinstance(field_info, str):
            # 简单字段名，使用精确匹配
            field = getattr(model, field_info)
            filters.append(field == value)
        elif isinstance(field_info, Callable):
            # 可调用对象，自定义过滤逻辑
            filter_condition = field_info(model, value)
            if filter_condition is not None:
                filters.append(filter_condition)
        elif isinstance(field_info, tuple):
            # 元组格式 (field_name, operation)
            field_name, operation = field_info
            field = getattr(model, field_name)
            if operation == "contains":
                filters.append(field.contains(value))
            elif operation == "==":
                filters.append(field == value)
            elif operation == "in_":
                filters.append(field.in_(value))
            elif operation == ">=":
                filters.append(field >= value)
            elif operation == "<=":
                filters.append(field <= value)

    return filters
