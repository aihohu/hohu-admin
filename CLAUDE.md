# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**HoHu Admin** is a FastAPI-based administrative backend with full-stack capabilities for building admin panels. It uses SQLAlchemy 2.0 with async support, PostgreSQL, Redis, and Alembic for database migrations.

**Key Technologies:**
- FastAPI (async web framework)
- SQLAlchemy 2.0 (async ORM)
- PostgreSQL (database)
- Redis (caching, sessions)
- Alembic (database migrations)
- Pydantic (data validation, automatic snake_case → camelCase conversion)
- Snowflake ID generator (distributed unique IDs)

## Development Commands

### Environment Setup
```bash
# Install dependencies (requires uv)
uv sync

# Activate virtual environment
source .venv/bin/activate
```

### Running the Application
```bash
# Development mode with hot reload
fastapi dev app/main.py

# Production mode
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Database Operations
```bash
# Create new migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head

# Rollback one migration
alembic downgrade -1
```

### Docker
```bash
# Build and start services
docker-compose up -d

# Stop services
docker-compose down
```

### Code Quality
```bash
# Lint and format (uses ruff)
ruff check .
ruff format .
```

### Testing
```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_main.py

# Run with coverage
pytest --cov=app tests/
```

## Architecture Overview

### Module Structure
The application follows a modular architecture under `app/modules/` with clear layer separation:

```
app/modules/
├── auth/              # Authentication & authorization
│   ├── api.py        # Auth endpoints (login, token refresh)
│   ├── service.py    # AuthService, get_current_user(), build_menu_tree()
│   └── schemas/      # Request/response models
├── system/            # System management (User, Role, Menu, Dict)
│   ├── api/          # API layer: handles HTTP requests/responses
│   ├── service/      # Service layer: business logic 🆕
│   │   ├── user_service.py
│   │   ├── role_service.py
│   │   └── menu_service.py
│   ├── models/       # Models layer: SQLAlchemy ORM models
│   └── schemas/      # Schema layer: Pydantic schemas
└── business/          # Placeholder for custom business modules
```

### Layer Responsibilities

**API Layer (`api/`):**
- Handle HTTP requests and responses
- Parse request parameters
- Call Service layer methods
- Manage database transactions (commit/rollback)
- Return formatted `ResponseModel` responses

**Service Layer (`service/`):**
- Implement business logic
- Validate business rules
- Handle data transformations
- Raise business exceptions
- Perform database queries (but don't commit - let API layer handle it)

**Models Layer (`models/`):**
- Define SQLAlchemy ORM models
- Specify table structures and relationships
- Use Snowflake IDs for primary keys

**Schema Layer (`schemas/`):**
- Define Pydantic models for request/response
- Handle data validation
- Automatic snake_case → camelCase conversion

### Key Architectural Patterns

**1. Authentication Flow:**
- Login through `/auth/login` → JWT token generated
- Token stored in header `Authorization: Bearer <token>`
- `get_current_user()` dependency validates JWT and preloads `roles.menus` for RBAC
- Redis blacklist for logout functionality (planned)

**2. RBAC (Role-Based Access Control):**
- Three-tier model: User → Role → Menu
- Menus have `permission` field (e.g., "sys:user:list")
- Use `check_permissions("sys:user:list")` or `require_permissions()` decorators
- Super admin users bypass all permission checks

**3. Database Session Management:**
- Async sessions via `get_db()` dependency
- Auto-commit on success, auto-rollback on error
- Connection pooling configured with `pool_pre_ping=True`
- **Important**: Service layer queries data but doesn't commit; API layer commits

**4. ID Generation:**
- All primary keys use Snowflake IDs via `next_id()` from `app.core.id_generator`
- IDs automatically serialized as strings in responses to prevent JavaScript BigInt precision loss

**5. Response Format:**
- All endpoints return `ResponseModel[T]` with `{code, msg, data}`
- Use `ResponseModel.success(data=...)` or `ResponseModel.error(msg=...)`

**6. Naming Convention:**
- **Backend (Python/SQLAlchemy)**: `snake_case` (e.g., `user_name`)
- **Frontend (JSON)**: `camelCase` (e.g., `userName`)
- **Auto-conversion**: Pydantic's `alias_generator=to_camel` handles conversion
- **Special case**: For fields like `i18n_key` that don't convert correctly, manually specify `Field(alias="i18nKey")`

**7. Service Layer Pattern:**
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
        # Build query logic here
        # Use paginate utility for pagination
        pass

    async def create(self, db: AsyncSession, example_in: ExampleCreate) -> Example:
        """创建示例"""
        # Validate business rules
        # Check uniqueness
        # Create and return the object (don't commit here)
        pass

    async def update(self, db: AsyncSession, id: int, example_in: ExampleCreate) -> Example:
        """更新示例"""
        # Get object
        # Update fields
        # Return the object (don't commit here)
        pass

    async def delete(self, db: AsyncSession, id: int) -> None:
        """删除示例"""
        # Get object
        # Check business rules (e.g., can't delete if used elsewhere)
        # Delete object (don't commit here)
        pass

# Create singleton
example_service = ExampleService()
```

**8. API Layer Pattern:**
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
    await db.commit()  # Commit at API layer
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

**9. Schema Patterns:**
```python
# Request schema
class UserCreate(BaseModel):
    user_name: str
    roles: list[str] = []  # Role codes, not IDs
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

# Response schema with ID serialization
class UserItemOut(BaseModel):
    user_id: int
    # ... other fields

    @field_serializer("user_id")
    def serialize_id(self, user_id: int, _info):
        return str(user_id)  # Prevent BigInt precision loss

    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
```

**10. Business Exception Pattern:**
```python
# Use specific business exceptions instead of HTTPException
from app.core.exceptions import (
    NotFoundException,
    DuplicateException,
    BusinessRuleException,
    AuthorizationException,
)

# In Service layer
if not user:
    raise UserNotFoundException()  # Specific exception

if user.status == "disabled":
    raise BusinessRuleException("用户已被禁用")

# The exception handler in app/core/exceptions.py will automatically
# convert these to proper HTTP responses
```

**11. Route Registration:**
- All routers registered in `app/main.py`
- Pattern: `app.include_router(router, prefix="/module/entity", tags=["Module"])`

## Quick Start: Adding a New Module

Use this template to rapidly create a new module with Service layer architecture:

### Step 1: Create Module Directory
```bash
mkdir -p app/modules/example/{api,service,models,schemas}
```

### Step 2: Create Model (`app/modules/example/models/example.py`)
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
    # ... other fields
```

### Step 3: Create Schemas (`app/modules/example/schemas/example.py`)
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
    # ... other fields
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ExampleUpdate(BaseModel):
    """更新请求"""
    name: str = None
    # ... other fields
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ExampleOut(BaseModel):
    """响应模型"""
    example_id: int
    name: str
    # ... other fields
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
```

### Step 4: Create Service (`app/modules/example/service/example_service.py`)
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
        # Build filters
        filters = build_filters(Example, {"name": ("name", "contains")}, **query.model_dump())

        # Paginate
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


# Create singleton
example_service = ExampleService()
```

### Step 5: Create API (`app/modules/example/api/example.py`)
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

### Step 6: Create `__init__.py` Files
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

### Step 7: Register Router in `app/main.py`
```python
from app.modules.example.api.example import router as example_router

# Register router
app.include_router(example_router, prefix="/system/example", tags=["示例管理"])
```

### Step 8: Create Database Migration
```bash
alembic revision --autogenerate -m "add example module"
alembic upgrade head
```

### Step 9: Add Business Exception (if needed)
```python
# In app/core/exceptions.py
class ExampleNotFoundException(NotFoundException):
    """示例不存在异常"""
    def __init__(self):
        super().__init__(resource_type="示例")
```

## Important Configuration Files

- **`.env`**: Environment variables (DATABASE_URL, SECRET_KEY, Redis settings)
- **`alembic.ini`**: Database migration configuration
- **`alembic/env.py`**: Async migration runner setup
- **`pyproject.toml`**: Ruff linting rules, pytest config, project metadata
- **`requirements.txt`**: Python dependencies
- **`app/constants/constants.py`**: Application-level constants

## Common Pitfalls

1. **ID Serialization**: Always serialize Snowflake IDs as strings in response schemas to prevent frontend BigInt precision loss
2. **Password Updates**: Never expose `hashed_password` in responses; only accept plain `password` in create/update operations
3. **Role Assignment**: Users receive roles by `role_code` (string), not `role_id` (integer)
4. **Database Commits**: The `get_db()` dependency handles commits/rollbacks automatically at API layer; Service layer should NOT commit
5. **Async Queries**: Always use `await` with async SQLAlchemy operations and `selectinload` for eager loading relationships
6. **Business Exceptions**: Use specific business exceptions from `app.core.exceptions` instead of `HTTPException` for better error handling
7. **Layer Separation**: Keep business logic in Service layer, HTTP handling in API layer - don't mix them
8. **Transaction Boundaries**: A single HTTP request should map to a single database transaction; commit only once at the end

## Code Style Guidelines

### Type Hints
- Use `Mapped[T]` for SQLAlchemy 2.0 model fields
- Use `list[T]` instead of `List[T]` for collection types
- Use `dict[K, V]` instead of `Dict[K, V]` for dictionary types

### Docstrings
- Use Google-style docstrings for functions and classes
- Include Args, Returns, and Raises sections where applicable
- Example:
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

### Error Messages
- Use consistent Chinese error messages across the application
- Use specific business exception classes for different error scenarios
- Error messages should be user-friendly and actionable
