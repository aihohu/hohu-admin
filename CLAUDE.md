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
The application follows a modular architecture under `app/modules/`:

```
app/modules/
├── auth/              # Authentication & authorization
│   ├── api.py        # Auth endpoints (login, token refresh)
│   ├── service.py    # AuthService, get_current_user(), build_menu_tree()
│   └── schemas/      # Request/response models
├── system/            # System management (User, Role, Menu, Dict)
│   ├── api/          # CRUD endpoints for each entity
│   ├── crud/         # Business logic layer
│   ├── models/       # SQLAlchemy ORM models
│   └── schemas/      # Pydantic schemas with automatic camelCase conversion
└── business/          # Placeholder for custom business modules
```

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

**7. Schema Patterns:**
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

**8. Route Registration:**
- All routers registered in `app/main.py`
- Pattern: `app.include_router(router, prefix="/module/entity", tags=["Module"])`

## Adding New Modules

1. Create directory under `app/modules/<module_name>/`
2. Define models in `models.py` using `app.db.base.Base` and `next_id()` for primary keys
3. Create schemas in `schemas.py` with `alias_generator=to_camel`
4. Implement API endpoints in `api.py` using `get_current_user` for auth
5. Register router in `app/main.py`
6. Create migration: `alembic revision --autogenerate -m "add <module>"`

## Important Configuration Files

- **`.env`**: Environment variables (DATABASE_URL, SECRET_KEY, Redis settings)
- **`alembic.ini`**: Database migration configuration
- **`alembic/env.py`**: Async migration runner setup
- **`pyproject.toml`**: Ruff linting rules, pytest config, project metadata
- **`requirements.txt`**: Python dependencies

## Common Pitfalls

1. **ID Serialization**: Always serialize Snowflake IDs as strings in response schemas to prevent frontend BigInt precision loss
2. **Password Updates**: Never expose `hashed_password` in responses; only accept plain `password` in create/update operations
3. **Role Assignment**: Users receive roles by `role_code` (string), not `role_id` (integer)
4. **Database Commits**: The `get_db()` dependency handles commits/rollbacks automatically
5. **Async Queries**: Always use `await` with async SQLAlchemy operations and `selectinload` for eager loading relationships
