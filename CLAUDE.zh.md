# CLAUDE.md (中文版)

本文件为 Claude Code (claude.ai/code) 在此代码库中工作时提供指导。

## 项目概述

**HoHu Admin** 是一个基于 FastAPI 的管理后台，具有构建管理面板的全栈能力。它使用 SQLAlchemy 2.0（支持异步）、PostgreSQL、Redis 和 Alembic 进行数据库迁移。

**核心技术:**
- FastAPI（异步 Web 框架）
- SQLAlchemy 2.0（异步 ORM）
- PostgreSQL（数据库）
- Redis（缓存、会话）
- Alembic（数据库迁移）
- Pydantic（数据验证、自动 snake_case → camelCase 转换）
- Snowflake ID 生成器（分布式唯一 ID）

## 开发命令

### 环境搭建
```bash
# 安装依赖（需要 uv）
uv sync

# 激活虚拟环境
source .venv/bin/activate
```

### 运行应用
```bash
# 开发模式（支持热重载）
fastapi dev app/main.py

# 生产模式
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 数据库操作
```bash
# 创建新的迁移文件
alembic revision --autogenerate -m "描述"

# 应用迁移
alembic upgrade head

# 回滚一个迁移
alembic downgrade -1
```

### Docker
```bash
# 构建并启动服务
docker-compose up -d

# 停止服务
docker-compose down
```

### 代码质量
```bash
# 代码检查和格式化（使用 ruff）
ruff check .
ruff format .
```

### 测试
```bash
# 运行所有测试
pytest

# 运行特定测试文件
pytest tests/test_main.py

# 运行测试并生成覆盖率报告
pytest --cov=app tests/
```

## 架构概览

### 模块结构
应用采用模块化架构，位于 `app/modules/` 下，具有清晰的层分离：

```
app/modules/
├── auth/              # 认证与授权
│   ├── api.py        # 认证接口（登录、刷新令牌）
│   ├── service.py    # AuthService、get_current_user()、build_menu_tree()
│   └── schemas/      # 请求/响应模型
├── system/            # 系统管理（User、Role、Menu、Dict）
│   ├── api/          # API 层：处理 HTTP 请求/响应
│   ├── service/      # Service 层：业务逻辑 🆕
│   │   ├── user_service.py
│   │   ├── role_service.py
│   │   └── menu_service.py
│   ├── models/       # Models 层：SQLAlchemy ORM 模型
│   └── schemas/      # Schema 层：Pydantic 模式
└── business/          # 自定义业务模块占位符
```

### 层职责

**API 层 (`api/`)：**
- 处理 HTTP 请求和响应
- 解析请求参数
- 调用 Service 层方法
- 管理数据库事务（提交/回滚）
- 返回格式化的 `ResponseModel` 响应

**Service 层 (`service/`)：**
- 实现业务逻辑
- 验证业务规则
- 处理数据转换
- 抛出业务异常
- 执行数据库查询（但不提交 - 让 API 层处理）

**Models 层 (`models/`)：**
- 定义 SQLAlchemy ORM 模型
- 指定表结构和关系
- 使用 Snowflake ID 作为主键

**Schema 层 (`schemas/`)：**
- 定义请求/响应的 Pydantic 模型
- 处理数据验证
- 自动 snake_case → camelCase 转换

### 核心架构模式

**1. 认证流程:**
- 通过 `/auth/login` 登录 → 生成 JWT 令牌
- 令牌存储在请求头 `Authorization: Bearer <token>`
- `get_current_user()` 依赖验证 JWT 并预加载 `roles.menus` 用于 RBAC
- Redis 黑名单用于退出登录功能（计划中）

**2. RBAC（基于角色的访问控制）:**
- 三层模型：用户 → 角色 → 菜单
- 菜单具有 `permission` 字段（如 "sys:user:list"）
- 使用 `require_permissions(perm_code="sys:user:list")` 或 `require_permissions(super_admin_only=True)` 装饰器
- 超级管理员（user_name="admin" 或 role_code="R_SUPER"）绕过所有权限检查

**3. 数据库会话管理:**
- 通过 `get_db()` 依赖提供异步会话
- 成功时自动提交，错误时自动回滚
- 连接池配置了 `pool_pre_ping=True`
- **重要**：Service 层查询数据但不提交；API 层负责提交

**4. ID 生成:**
- 所有主键使用 Snowflake ID，通过 `app.core.id_generator` 的 `next_id()` 生成
- 响应中 ID 自动序列化为字符串，防止 JavaScript BigInt 精度丢失

**5. 响应格式:**
- 所有接口返回 `ResponseModel[T]`，格式为 `{code, msg, data}`
- 使用 `ResponseModel.success(data=...)` 或 `ResponseModel.error(msg=...)`

**6. 命名规范:**
- **后端（Python/SQLAlchemy）**: `snake_case`（如 `user_name`）
- **前端（JSON）**: `camelCase`（如 `userName`）
- **自动转换**: Pydantic 的 `alias_generator=to_camel` 处理转换
- **特殊情况**: 对于 `i18n_key` 等无法正确转换的字段，手动指定 `Field(alias="i18nKey")`

**7. Service 层模式:**
```python
# app/modules/system/service/example_service.py
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException, NotFoundException
from app.modules.system.models.example import Example
from app.modules.system.schemas.example import ExampleCreate, ExampleQuery

class ExampleService:
    """示例业务逻辑服务"""

    async def get_list(self, db: AsyncSession, query: ExampleQuery):
        """获取分页列表"""
        # 在这里构建查询逻辑
        # 使用分页工具进行分页
        pass

    async def create(self, db: AsyncSession, example_in: ExampleCreate) -> Example:
        """创建示例"""
        # 验证业务规则
        # 检查唯一性
        # 创建并返回对象（不要在这里提交）
        pass

    async def update(self, db: AsyncSession, id: int, example_in: ExampleCreate) -> Example:
        """更新示例"""
        # 获取对象
        # 更新字段
        # 返回对象（不要在这里提交）
        pass

    async def delete(self, db: AsyncSession, id: int) -> None:
        """删除示例"""
        # 获取对象
        # 检查业务规则（例如，在其他地方使用时不能删除）
        # 删除对象（不要在这里提交）
        pass

# 创建单例
example_service = ExampleService()
```

**8. API 层模式:**
```python
# app/modules/system/api/example.py
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.models.user import User
from app.modules.system.schemas.example import ExampleCreate, ExampleOut, ExampleQuery
from app.modules.system.service.example_service import example_service

router = APIRouter()

@router.get("/list", response_model=ResponseModel[PageResult[ExampleOut]])
async def get_list(
    query: ExampleQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """获取示例分页列表"""
    page_data = await example_service.get_list(db, query)
    return ResponseModel.success(data=page_data)

@router.post("/add")
async def add(
    example_in: ExampleCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """创建示例"""
    await example_service.create(db, example_in)
    await db.commit()  # 在 API 层提交
    return ResponseModel.success(msg="创建成功")

@router.put("/{id}")
async def update(
    id: int,
    example_in: ExampleCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """更新示例"""
    await example_service.update(db, id, example_in)
    await db.commit()
    return ResponseModel.success(msg="更新成功")

@router.delete("/{id}")
async def delete(
    id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """删除示例"""
    await example_service.delete(db, id)
    await db.commit()
    return ResponseModel.success(msg="删除成功")
```

**9. Schema 模式:**
```python
# 请求 schema
class UserCreate(BaseModel):
    user_name: str
    roles: list[str] = []  # 角色代码，不是 ID
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

# 带有 ID 序列化的响应 schema
class UserItemOut(BaseModel):
    user_id: int
    # ... 其他字段

    @field_serializer("user_id")
    def serialize_id(self, user_id: int, _info):
        return str(user_id)  # 防止 BigInt 精度丢失

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
```

**10. 业务异常模式:**
```python
# 使用特定的业务异常而不是 HTTPException
from app.core.exceptions import (
    NotFoundException,
    DuplicateException,
    BusinessRuleException,
    AuthorizationException,
)

# 在 Service 层中
if not user:
    raise UserNotFoundException()  # 特定异常

if user.status == "disabled":
    raise BusinessRuleException("用户已被禁用")

# app/core/exceptions.py 中的异常处理器会自动
# 将这些转换为正确的 HTTP 响应
```

**11. 路由注册:**
- 所有路由在 `app/main.py` 中注册
- 模式: `app.include_router(router, prefix="/module/entity", tags=["Module"])`

## 快速开始：添加新模块

使用此模板快速创建具有 Service 层架构的新模块：

### 步骤 1：创建模块目录
```bash
mkdir -p app/modules/example/{api,service,models,schemas}
```

### 步骤 2：创建模型 (`app/modules/example/models/example.py`)
```python
from sqlalchemy import Column, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.core.id_generator import next_id


class Example(Base):
    """示例模型"""
    __tablename__ = "example"

    example_id: Mapped[int] = mapped_column(
        primary_key=True, default=next_id, comment="示例ID"
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="名称")
    # ... 其他字段
```

### 步骤 3：创建 Schema (`app/modules/example/schemas/example.py`)
```python
from pydantic import BaseModel, ConfigDict

from app.utils.field_alias import to_camel


class ExampleQuery(BaseModel):
    """查询参数"""
    name: str = None
    current: int = 10
    size: int = 10
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ExampleCreate(BaseModel):
    """创建请求"""
    name: str
    # ... 其他字段
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ExampleUpdate(BaseModel):
    """更新请求"""
    name: str = None
    # ... 其他字段
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ExampleOut(BaseModel):
    """响应模型"""
    example_id: int
    name: str
    # ... 其他字段
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
```

### 步骤 4：创建 Service (`app/modules/example/service/example_service.py`)
```python
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ExampleNotFoundException
from app.modules.system.models.example import Example
from app.modules.system.schemas.example import ExampleCreate, ExampleUpdate
from app.utils.pagination import build_filters, paginate


class ExampleService:
    """示例业务逻辑服务"""

    async def get_list(self, db: AsyncSession, query: ExampleQuery):
        """获取分页列表"""
        # 构建过滤条件
        filters = build_filters(Example, {"name": ("name", "contains")}, **query.model_dump())

        # 分页
        return await paginate(
            db=db,
            model=Example,
            query_params=query,
            filters=filters,
            order_by=Example.create_time.desc(),
        )

    async def create(self, db: AsyncSession, example_in: ExampleCreate) -> Example:
        """创建示例"""
        new_example = Example(**example_in.model_dump())
        db.add(new_example)
        return new_example

    async def update(self, db: AsyncSession, example_id: int, example_in: ExampleUpdate) -> Example:
        """更新示例"""
        example = await db.get(Example, example_id)
        if not example:
            raise ExampleNotFoundException()

        update_data = example_in.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(example, field, value)

        return example

    async def delete(self, db: AsyncSession, example_id: int) -> None:
        """删除示例"""
        example = await db.get(Example, example_id)
        if not example:
            raise ExampleNotFoundException()
        await db.delete(example)


# 创建单例
example_service = ExampleService()
```

### 步骤 5：创建 API (`app/modules/example/api/example.py`)
```python
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.models.user import User
from app.modules.system.schemas.example import ExampleCreate, ExampleOut, ExampleQuery
from app.modules.system.service.example_service import example_service

router = APIRouter()

@router.get("/list", response_model=ResponseModel[PageResult[ExampleOut]])
async def get_list(
    query: ExampleQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """获取示例分页列表"""
    page_data = await example_service.get_list(db, query)
    return ResponseModel.success(data=page_data)

@router.post("/add")
async def add(
    example_in: ExampleCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """创建示例"""
    await example_service.create(db, example_in)
    await db.commit()
    return ResponseModel.success(msg="创建成功")

@router.put("/{example_id}")
async def update(
    example_id: int,
    example_in: ExampleCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """更新示例"""
    await example_service.update(db, example_id, example_in)
    await db.commit()
    return ResponseModel.success(msg="更新成功")

@router.delete("/{example_id}")
async def delete(
    example_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """删除示例"""
    await example_service.delete(db, example_id)
    await db.commit()
    return ResponseModel.success(msg="删除成功")
```

### 步骤 6：创建 `__init__.py` 文件
```python
# app/modules/example/__init__.py
"""示例模块"""

from app.modules.system.api.example import router

__all__ = ["router"]
```

```python
# app/modules/example/service/__init__.py
"""Service 层"""

from app.modules.example.service.example_service import example_service

__all__ = ["example_service"]
```

### 步骤 7：在 `app/main.py` 中注册路由
```python
from app.modules.example.api.example import router as example_router

# 注册路由
app.include_router(example_router, prefix="/system/example", tags=["示例管理"])
```

### 步骤 8：创建数据库迁移
```bash
alembic revision --autogenerate -m "add example module"
alembic upgrade head
```

### 步骤 9：添加业务异常（如果需要）
```python
# 在 app/core/exceptions.py 中
class ExampleNotFoundException(NotFoundException):
    """示例不存在异常"""
    def __init__(self):
        super().__init__(resource_type="示例")
```

## 重要配置文件

- **`.env`**: 环境变量（DATABASE_URL、SECRET_KEY、Redis 配置）
- **`alembic.ini`**: 数据库迁移配置
- **`alembic/env.py`**: 异步迁移运行器设置
- **`pyproject.toml`**: Ruff 代码检查规则、pytest 配置、项目元数据
- **`requirements.txt`**: Python 依赖
- **`app/constants/constants.py`**: 应用级常量

## 常见陷阱

1. **ID 序列化**: 在响应 schema 中始终将 Snowflake ID 序列化为字符串，防止前端 BigInt 精度丢失
2. **密码更新**: 永远不要在响应中暴露 `hashed_password`；只在创建/更新操作中接受明文 `password`
3. **角色分配**: 用户通过 `role_code`（字符串）接收角色，而不是 `role_id`（整数）
4. **数据库提交**: `get_db()` 依赖在 API 层自动处理提交/回滚；Service 层不应该提交
5. **异步查询**: 始终对异步 SQLAlchemy 操作使用 `await`，并使用 `selectinload` 进行关系预加载
6. **业务异常**: 使用 `app.core.exceptions` 中的特定业务异常，而不是在业务逻辑中使用 `HTTPException`
7. **层分离**: 将业务逻辑保留在 Service 层，HTTP 处理保留在 API 层 - 不要混合它们
8. **事务边界**: 单个 HTTP 请求应该对应单个数据库事务；只在最后提交一次

## 代码风格指南

### 类型提示
- 对 SQLAlchemy 2.0 模型字段使用 `Mapped[T]`
- 对集合类型使用 `list[T]` 而不是 `List[T]`
- 对字典类型使用 `dict[K, V]` 而不是 `Dict[K, V]`

### 文档字符串
- 对函数和类使用 Google 风格的文档字符串
- 在适用的地方包含 Args、Returns 和 Raises 部分
- 示例：
```python
def get_user(user_id: int) -> User:
    """
    获取用户信息

    Args:
        user_id: 用户ID

    Returns:
        用户对象

    Raises:
        UserNotFoundException: 用户不存在
    """
    pass
```

### 错误消息
- 在整个应用中使用一致的中文错误消息
- 对不同的错误场景使用特定的业务异常类
- 错误消息应该对用户友好且可操作
