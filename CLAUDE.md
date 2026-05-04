# CLAUDE.md

## Project Overview

FastAPI admin backend with async SQLAlchemy 2.0, PostgreSQL, Redis, Alembic.

**Tech Stack:** FastAPI / SQLAlchemy 2.0 (async) / PostgreSQL / Redis / Alembic / Pydantic v2 / Snowflake ID / bcrypt / python-jose (JWT)

**Requirements:** Python >= 3.12, uv

## Commands

```bash
uv sync                                    # Install dependencies
fastapi dev app/main.py                    # Dev server (port 8000, hot reload)
alembic revision --autogenerate -m "desc"  # Create migration
alembic upgrade head                       # Apply migrations
ruff check . && ruff format .              # Lint + format
pytest                                     # Run tests
python scripts/init_db.py                  # Seed admin user and menus
```

**Must run `ruff check . && ruff format .` after code changes.**

## Project Structure

```
app/
├── main.py                    # Entry point, lifespan, router registration
├── core/
│   ├── auth.py                # require_permissions() dependency
│   ├── base_response.py       # ResponseModel[T], PageResult[T]
│   ├── config.py              # Pydantic Settings (from .env)
│   ├── exceptions.py          # Exception hierarchy + global handlers
│   ├── id_generator.py        # Snowflake ID (next_id())
│   ├── redis.py               # Async Redis connection pool
│   └── security.py            # bcrypt + JWT create/verify
├── db/
│   ├── base.py                # DeclarativeBase + association tables
│   └── session.py             # Async engine, get_db() dependency
├── modules/
│   ├── auth/
│   │   ├── api.py             # Auth endpoints
│   │   ├── service.py         # AuthService, get_current_user(), build_menu_tree()
│   │   └── schemas/           # LoginCredentials, Token, etc.
│   └── system/
│       ├── api/               # user.py, role.py, menu.py, dict_type.py, dict_data.py
│       ├── models/            # User, Role, Menu, DictType, DictData
│       ├── schemas/           # Pydantic schemas per entity
│       └── service/           # Service singletons per entity
└── utils/
    ├── pagination.py          # paginate(), build_filters(), QueryParams
    └── mask_util.py           # MaskUtil (phone, email, id_card masking)
```

## Architecture: API → Service → Model

### API Layer
- Handle HTTP, parse params, call Service, **commit transactions** (`await db.commit()`)
- Return `ResponseModel.success(data=...)` or `ResponseModel.error(msg=...)`

### Service Layer
- Business logic, raise domain exceptions, perform DB queries
- **Never commit** — let API layer handle it
- Module-level singletons: `user_service = UserService()`

### Model Layer
- SQLAlchemy 2.0 `Mapped[T]`, PKs use `default=next_id` (Snowflake)
- All models import `Base` from `app.db.base` (has association tables)

### Schema Layer
- Pydantic v2 with `alias_generator=to_camel` for auto snake_case → camelCase
- BigInteger IDs serialized as strings via `@field_serializer` (prevent JS BigInt loss)

## Response Format

```json
{"code": 200, "msg": "success", "data": <T>}
{"code": 200, "msg": "success", "data": {"records": [...], "total": 100, "current": 1, "size": 10}}
{"code": 400, "msg": "error message", "data": null}
```

## Authentication & Authorization

- **Login:** `POST /auth/login` → JWT (HS256, 7-day expiry)
- **Token:** `Authorization: Bearer <token>`
- **`get_current_user()`**: in `app/modules/auth/service.py` (NOT `app/core/auth.py`)
- **RBAC:** User → Role → Menu (three-tier)
- **Permission check:** `require_permissions("sys:user:list")` or `require_permissions(super_admin_only=True)` in `app/core/auth.py`
- **Super admin:** `user_name == "admin"` or `R_SUPER` in role codes → bypasses all checks

## Exception Hierarchy

```
BusinessException (base, has error_code for frontend i18n)
├── NotFoundException(resource_type) — reusable, pass resource name
├── DuplicateException(field, value) — reusable
├── AuthenticationException (401)
├── AuthorizationException (403)
└── BusinessRuleException(message) — reusable for general business rules
    └── InvalidParameterException(message)
```

**Rules:**
- **Never use `HTTPException`** in business logic — use classes from `app/core/exceptions.py`
- **Reuse generic exceptions** (`NotFoundException("资源名")`, `DuplicateException("字段", "值")`) instead of creating new subclasses per entity
- Only create a new subclass when it needs unique logic (not just a different message)
- **Always set `error_code`** for frontend i18n mapping — use `UPPER_SNAKE_CASE` (e.g., `AI_PROVIDER_NOT_FOUND`), response includes `errorCode` field automatically
- Frontend maps `errorCode` via `$t('errorCode.XXX')`; if no mapping exists, falls back to backend `msg`

## Pagination Utilities

- `QueryParams(BaseModel)` — base class with `current` (default 1), `size` (default 10)
- `paginate(db, model, query_params, filters, order_by, eager_loads)` — generic paginated query
- `build_filters(model, field_mapping, **kwargs)` — maps params to SQLAlchemy filters
  - String: exact match
  - Tuple `(field, op)`: `"contains"`, `"=="`, `"in_"`, `">="`, `"<="`
  - Callable: custom filter

## Adding a New Module

1. Create directory: `app/modules/example/{api,service,models,schemas}`
2. **Model:** `Mapped[T]` fields, PK with `default=next_id`, import `Base` from `app.db.base`
3. **Schema:** `alias_generator=to_camel`, `@field_serializer` for IDs as strings
4. **Service:** Business logic, never commit, raise domain exceptions, module-level singleton
5. **API:** Call service, `await db.commit()` for writes, return `ResponseModel`
6. Register router in `app/main.py`: `app.include_router(router, prefix="/...", tags=[...])`
7. Add exception subclasses in `app/core/exceptions.py` only if needed — prefer reusing generic ones with `resource_type` param
8. Set `error_code` on exceptions for frontend i18n: `exc.error_code = "MODULE_RESOURCE_NOT_FOUND"`
9. Run migration: `alembic revision --autogenerate -m "..." && alembic upgrade head`

## Common Pitfalls

1. **ID serialization:** Always `@field_serializer` Snowflake IDs to `str` in response schemas
2. **Commit separation:** Service never commits; API calls `await db.commit()`
3. **`get_current_user` location:** In `app/modules/auth/service.py`, NOT `app/core/auth.py`
4. **Base import:** Models import `Base` from `app.db.base` (has association tables), not `app.db.session`
5. **to_camel import:** `from pydantic.alias_generators import to_camel`
6. **No HTTPException:** Use domain exceptions from `app/core/exceptions.py`
7. **Reuse exceptions:** Use `NotFoundException("资源名")` directly instead of creating per-entity subclasses
7. **Password handling:** Never expose `hashed_password` in responses; accept plain `password` in create/update
8. **Role assignment:** Users receive roles by `role_code` (string), not `role_id`
9. **Async queries:** Always `await`; use `selectinload` for eager loading relationships
10. **i18n_key field:** May need manual `Field(alias="i18nKey")` if auto-conversion is incorrect
