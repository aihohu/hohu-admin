# 分页查询开发指南

本文档详细介绍了 HoHu Admin 分页查询工具的使用方法，包括基础查询、复杂查询和各种实际场景的完整示例。

## 目录

- [概述](#概述)
- [快速开始](#快速开始)
- [基础查询](#基础查询)
- [复杂查询](#复杂查询)
- [最佳实践](#最佳实践)
- [常见问题](#常见问题)

## 概述

HoHu Admin 提供了两套分页查询工具：

### 1. `paginate()` - 基础分页函数

适用于标准的 CRUD 操作，简单易用。

**特点**：
- ✅ 自动处理查询条件、排序、分页
- ✅ 支持字段映射构建过滤条件
- ✅ 支持预加载关联数据
- ✅ 类型安全，有完整的类型提示

### 2. `paginate_custom()` - 自定义分页函数

适用于复杂的 SQL 查询场景。

**特点**：
- ✅ 支持任意复杂的 SQLAlchemy 查询
- ✅ 支持自定义计数查询
- ✅ 支持 JOIN、子查询、聚合查询
- ✅ 灵活处理各种查询场景

## 快速开始

### 安装依赖

分页工具位于 `app/utils/pagination.py`，无需额外安装依赖。

### 导入模块

```python
# 基础分页
from app.utils.pagination import paginate, build_filters

# 复杂查询分页
from app.utils.pagination import paginate_custom
```

## 基础查询

### 1. 简单列表查询

最基础的使用方式，适合不需要过滤条件的简单列表：

```python
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.models.user import User
from app.modules.system.schemas.user import UserItemOut
from app.utils.pagination import paginate

router = APIRouter()

@router.get("/list", response_model=ResponseModel[PageResult[UserItemOut]])
async def get_user_list(
    current: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
):
    # 使用基础分页函数
    page_data = await paginate(
        db=db,
        model=User,
        query_params=type("Query", (), {"current": current, "size": size})(),
        order_by=User.create_time.desc(),
    )

    return ResponseModel.success(data=page_data)
```

### 2. 使用 Query Schema

推荐使用 Pydantic Schema 来定义查询参数：

```python
from pydantic import BaseModel
from pydantic.alias_generators import to_camel

class UserQuery(BaseModel):
    """用户查询参数"""
    current: int = 1
    size: int = 10
    user_name: str | None = None
    status: str | None = None

    model_config = type("Config", (), {"alias_generator": to_camel, "populate_by_name": True})()

@router.get("/list", response_model=ResponseModel[PageResult[UserItemOut]])
async def get_user_list(
    query: UserQuery = Depends(),
    db: AsyncSession = Depends(get_db),
):
    page_data = await paginate(
        db=db,
        model=User,
        query_params=query,
        order_by=User.create_time.desc(),
    )

    return ResponseModel.success(data=page_data)
```

### 3. 使用 `build_filters()` 构建查询条件

使用字段映射自动构建过滤条件：

```python
from app.utils.pagination import build_filters

@router.get("/list")
async def get_user_list(query: UserQuery = Depends(), db: AsyncSession = Depends(get_db)):
    # 定义字段映射
    field_mapping = {
        "user_name": ("user_name", "contains"),  # 模糊匹配
        "status": ("status", "=="),               # 精确匹配
        "role_id": ("role_id", "in_"),          # 在列表中
    }

    # 构建过滤条件
    filters = build_filters(User, field_mapping, **query.model_dump())

    # 分页查询
    page_data = await paginate(
        db=db,
        model=User,
        query_params=query,
        filters=filters,
        order_by=User.create_time.desc(),
    )

    return ResponseModel.success(data=page_data)
```

### 4. 预加载关联数据

避免 N+1 查询问题：

```python
from sqlalchemy.orm import selectinload

@router.get("/list")
async def get_user_list(query: UserQuery = Depends(), db: AsyncSession = Depends(get_db)):
    page_data = await paginate(
        db=db,
        model=User,
        query_params=query,
        order_by=User.create_time.desc(),
        eager_loads=[selectinload(User.roles)],  # 预加载角色信息
    )

    return ResponseModel.success(data=page_data)
```

### 5. 数据格式转换

如果需要对返回数据进行特殊处理：

```python
@router.get("/list")
async def get_user_list(
    query: UserQuery = Depends(),
    db: AsyncSession = Depends(get_db),
):
    # 执行分页查询
    page_data = await paginate(
        db=db,
        model=User,
        query_params=query,
        order_by=User.create_time.desc(),
        eager_loads=[selectinload(User.roles)],
    )

    # 转换数据格式
    user_list = []
    for u in page_data.records:
        item = UserItemOut.model_validate(u)
        # 特殊处理：只返回角色编码
        item.roles = [r.role_code for r in u.roles]
        user_list.append(item)

    # 返回转换后的数据
    return ResponseModel.success(
        data=PageResult(
            records=user_list,
            total=page_data.total,
            current=page_data.current,
            size=page_data.size,
        )
    )
```

## 复杂查询

### 1. JOIN 查询分页

适用于需要关联多个表的查询：

```python
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult, ResponseModel
from app.utils.pagination import paginate_custom
from app.modules.system.models.user import User
from app.modules.system.models.role import Role

@router.get("/users-with-roles")
async def get_users_with_roles(
    keyword: str = "",
    current: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
):
    # 构建 JOIN 查询
    stmt = (
        select(User, Role)
        .join(Role, User.roles)
        .where(
            or_(
                User.user_name.contains(keyword),
                User.nickname.contains(keyword),
                Role.role_name.contains(keyword),
            )
        )
        .order_by(User.create_time.desc())
    )

    # 使用 paginate_custom 进行分页
    page_data = await paginate_custom(
        db=db,
        query=stmt,
        current=current,
        size=size,
    )

    return ResponseModel.success(data=page_data)
```

### 2. 复杂多条件查询

适用于有多个复杂过滤条件的场景：

```python
from datetime import datetime

@router.get("/complex-search")
async def complex_search(
    start_date: datetime = None,
    end_date: datetime = None,
    role_codes: list[str] = [],
    status: str = None,
    current: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
):
    # 构建复杂查询条件
    conditions = []

    if start_date:
        conditions.append(User.create_time >= start_date)
    if end_date:
        conditions.append(User.create_time <= end_date)
    if status:
        conditions.append(User.status == status)

    # 主查询
    stmt = (
        select(User)
        .join(Role, User.roles)
        .where(and_(*conditions))
        .where(Role.role_code.in_(role_codes))
        .order_by(User.create_time.desc())
    )

    # 自定义计数查询（避免子查询问题）
    count_stmt = (
        select(func.count(User.user_id))
        .join(Role, User.roles)
        .where(and_(*conditions))
        .where(Role.role_code.in_(role_codes))
    )

    # 使用自定义计数查询分页
    page_data = await paginate_custom(
        db=db,
        query=stmt,
        count_query=count_stmt,  # 显式提供计数查询
        current=current,
        size=size,
    )

    return ResponseModel.success(data=page_data)
```

### 3. 聚合查询（GROUP BY + COUNT）

适用于统计类查询：

```python
@router.get("/user-stats")
async def get_user_statistics(
    role_id: int = None,
    current: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
):
    # 聚合查询：按角色统计用户数
    stmt = (
        select(
            Role.role_id,
            Role.role_name,
            func.count(User.user_id).label("user_count")
        )
        .outerjoin(User, Role.users)
        .group_by(Role.role_id, Role.role_name)
        .order_by(func.count(User.user_id).desc())
    )

    # 计数查询：统计分组数量
    count_stmt = (
        select(func.count())
        .select_from(
            select(Role.role_id)
            .outerjoin(User, Role.users)
            .group_by(Role.role_id)
            .alias("grouped")
        )
    )

    page_data = await paginate_custom(
        db=db,
        query=stmt,
        count_query=count_stmt,
        current=current,
        size=size,
    )

    return ResponseModel.success(data=page_data)
```

### 4. 子查询 + 分页

适用于需要使用子查询过滤的场景：

```python
from app.models import UserDepartment

@router.get("/users-in-depts")
async def get_users_in_departments(
    dept_ids: list[int],
    current: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
):
    # 子查询：找出这些部门的用户ID
    subquery = (
        select(UserDepartment.user_id)
        .where(UserDepartment.dept_id.in_(dept_ids))
        .distinct()
        .alias("dept_users")
    )

    # 主查询：基于子查询过滤用户
    stmt = (
        select(User)
        .where(User.user_id.in_(subquery))
        .order_by(User.create_time.desc())
    )

    count_stmt = select(func.count()).select_from(subquery)

    page_data = await paginate_custom(
        db=db,
        query=stmt,
        count_query=count_stmt,
        current=current,
        size=size,
    )

    return ResponseModel.success(data=page_data)
```

### 5. 多表关联查询

适用于需要关联多个表的复杂查询：

```python
@router.get("/order-list")
async def get_order_list(
    user_name: str = "",
    start_time: datetime = None,
    current: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
):
    # 关联用户、订单、商品三张表
    stmt = (
        select(Order, User, Product)
        .join(User, Order.user_id == User.user_id)
        .join(Product, Order.product_id == Product.product_id)
        .where(
            and_(
                User.user_name.contains(user_name) if user_name else True,
                Order.create_time >= start_time if start_time else True,
            )
        )
        .order_by(Order.create_time.desc())
    )

    # 自定义计数查询
    count_stmt = (
        select(func.count(Order.order_id))
        .join(User, Order.user_id == User.user_id)
        .where(
            and_(
                User.user_name.contains(user_name) if user_name else True,
                Order.create_time >= start_time if start_time else True,
            )
        )
    )

    page_data = await paginate_custom(
        db=db,
        query=stmt,
        count_query=count_stmt,
        current=current,
        size=size,
    )

    return ResponseModel.success(data=page_data)
```

### 6. 原生 SQL 查询（最灵活）

对于极端复杂的查询，可以直接使用原生 SQL：

```python
from sqlalchemy import text

@router.get("/native-sql")
async def native_sql_query(
    keyword: str = "",
    current: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
):
    offset = (current - 1) * size

    # 原生 SQL 查询
    sql = text("""
        SELECT u.*, r.role_name
        FROM sys_user u
        LEFT JOIN sys_user_role ur ON u.user_id = ur.user_id
        LEFT JOIN sys_role r ON ur.role_id = r.role_id
        WHERE u.user_name LIKE :keyword
           OR u.nickname LIKE :keyword
        ORDER BY u.create_time DESC
        LIMIT :limit OFFSET :offset
    """)

    # 计数 SQL
    count_sql = text("""
        SELECT COUNT(*)
        FROM sys_user u
        WHERE u.user_name LIKE :keyword
           OR u.nickname LIKE :keyword
    """)

    # 执行查询
    params = {"keyword": f"%{keyword}%", "limit": size, "offset": offset}
    result = await db.execute(sql, params)
    records = result.all()

    # 执行计数
    count_result = await db.execute(count_sql, {"keyword": f"%{keyword}%"})
    total = count_result.scalar() or 0

    # 手动包装分页结果
    from app.core.base_response import PageResult
    return ResponseModel.success(
        data=PageResult(
            records=records,
            total=total,
            current=current,
            size=size,
        )
    )
```

## 最佳实践

### 1. 选择合适的分页函数

| 场景 | 推荐方案 |
|------|----------|
| 简单 CRUD 查询 | `paginate()` + `build_filters()` |
| 包含 JOIN 的查询 | `paginate_custom()` |
| 聚合查询（GROUP BY） | `paginate_custom()` + 自定义 `count_query` |
| 复杂子查询 | `paginate_custom()` + 自定义 `count_query` |
| 极端复杂的 SQL | 原生 SQL + 手动分页 |

### 2. 使用字段映射简化代码

```python
# ❌ 不推荐：手动构建条件
filters = []
if query.user_name:
    filters.append(User.user_name.contains(query.user_name))
if query.status:
    filters.append(User.status == query.status)
# ...

# ✅ 推荐：使用字段映射
field_mapping = {
    "user_name": ("user_name", "contains"),
    "status": ("status", "=="),
}
filters = build_filters(User, field_mapping, **query.model_dump())
```

### 3. 避免自动推断计数查询

对于复杂查询，建议显式提供 `count_query`：

```python
# ❌ 不推荐：依赖自动推断
page_data = await paginate_custom(db, stmt, current=1, size=10)

# ✅ 推荐：显式提供计数查询
count_stmt = select(func.count(User.user_id))
page_data = await paginate_custom(db, stmt, count_query=count_stmt, current=1, size=10)
```

### 4. 合理使用预加载

```python
# ✅ 正确：预加载需要的关联
page_data = await paginate(
    db=db,
    model=User,
    query_params=query,
    eager_loads=[selectinload(User.roles)],
)

# ❌ 错误：预加载不需要的关联（增加数据库压力）
page_data = await paginate(
    db=db,
    model=User,
    query_params=query,
    eager_loads=[selectinload(User.roles), selectinload(User.logs)],
)
```

### 5. 添加必要的索引

确保查询字段有适当的索引：

```sql
-- 常用查询字段
CREATE INDEX idx_user_create_time ON sys_user(create_time);
CREATE INDEX idx_user_status ON sys_user(status);
CREATE INDEX idx_user_name ON sys_user(user_name);

-- 组合索引
CREATE INDEX idx_user_status_time ON sys_user(status, create_time);
```

## 常见问题

### Q1: 为什么 `paginate()` 只适用于简单查询？

A: `paginate()` 针对单表查询优化，自动处理分页逻辑。对于复杂查询（JOIN、子查询、聚合），推荐使用 `paginate_custom()`。

### Q2: 如何处理 `BigInt` 精度丢失问题？

A: HoHu Admin 使用 Snowflake ID，所有主键在 Schema 中已自动序列化为字符串：

```python
class UserItemOut(BaseModel):
    user_id: int

    @field_serializer("user_id")
    def serialize_id(self, user_id: int, _info):
        return str(user_id)  # 自动转为字符串
```

### Q3: 分页查询性能慢怎么办？

A: 检查以下几点：
1. 查询字段是否有索引
2. 是否使用了预加载避免 N+1 查询
3. 计数查询是否合理
4. 考虑使用游标分页代替 offset 分页

### Q4: 如何实现游标分页？

A: 游标分页适用于大数据量场景，示例：

```python
@router.get("/cursor-page")
async def cursor_page(
    last_id: int = 0,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(User)
        .where(User.user_id > last_id)
        .order_by(User.user_id.asc())
        .limit(size)
    )

    result = await db.execute(stmt)
    records = result.scalars().all()

    return ResponseModel.success(data=records)
```

### Q5: 复杂查询的 count 如何优化？

A: 对于复杂查询，可以：
1. 使用估算查询（EXPLAIN ANALYZE）
2. 缓存计数结果
3. 考虑使用近似计数
4. 优化 SQL 结构，避免不必要的 JOIN

```python
# 优化计数查询
count_stmt = (
    select(func.count(User.user_id))
    .select_from(
        select(User.user_id)
        .join(Role, User.roles)
        .where(conditions)
        .alias("subquery")
    )
)
```

## API 参考

### `paginate()` 函数

```python
async def paginate(
    db: AsyncSession,
    model: Any,
    query_params: QueryParams,
    filters: list[Any] | None = None,
    order_by: Any = None,
    eager_loads: list[Any] | None = None,
) -> PageResult
```

**参数**：
- `db`: 数据库会话
- `model`: SQLAlchemy 模型类
- `query_params`: 查询参数（包含 `current`, `size`）
- `filters`: 查询条件列表
- `order_by`: 排序条件（如 `Model.field.desc()`）
- `eager_loads`: 预加载的关联关系列表

**返回**：`PageResult` 对象

### `paginate_custom()` 函数

```python
async def paginate_custom(
    db: AsyncSession,
    query: Select,
    count_query: Select | None = None,
    current: int = 1,
    size: int = 10,
) -> PageResult
```

**参数**：
- `db`: 数据库会话
- `query`: 已构建的 SQLAlchemy 查询对象
- `count_query`: 可选的自定义计数查询
- `current`: 当前页码
- `size`: 每页大小

**返回**：`PageResult` 对象

### `build_filters()` 函数

```python
def build_filters(
    model: Any,
    field_mapping: dict[str, str],
    **kwargs,
) -> list[Any]
```

**参数**：
- `model`: SQLAlchemy 模型类
- `field_mapping`: 字段映射字典
- `**kwargs`: 查询参数键值对

**返回**：查询条件列表

### `PageResult` 类

```python
class PageResult[T](BaseModel):
    records: list[T]  # 数据记录
    total: int       # 总记录数
    current: int     # 当前页码
    size: int       # 每页大小
```

## 相关文档

- [项目 README](../README.md)
- [CLAUDE.md](../CLAUDE.md)
- [API 文档](http://127.0.0.1:8000/docs)
