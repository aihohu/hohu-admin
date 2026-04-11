# 🥞 HoHu Admin


<div align="center">
  <span>English | <a href="./README.md">中文</a></span>
</div>

[![license](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![github stars](https://img.shields.io/github/stars/aihohu/hohu-admin)](https://github.com/aihohu/hohu-admin)
[![github forks](https://img.shields.io/github/forks/aihohu/hohu-admin)](https://github.com/aihohu/hohu-admin)
---

**HoHu Admin** is a modern, high-performance, and modular backend admin system template built with **Python** and **FastAPI**. It uses **SQLAlchemy 2.0 (async)** as its core ORM and is designed specifically for decoupled frontend-backend architectures. Out of the box, it provides a complete production-ready backend infrastructure—including user authentication, role-based access control (RBAC), distributed ID generation, database migrations, logging & monitoring, and integrated API documentation.

In an era where AI applications are rapidly being deployed, **HoHu Admin** aims to free developers from repetitive low-level scaffolding tasks so they can focus on business innovation and intelligent integration. Whether you're validating a rapid prototype or building a scalable enterprise-grade application, HoHu Admin significantly lowers technical barriers, shortens development cycles, and enhances code quality and system security—empowering developers to embrace the AI era with ease.

## ✨ Key Features

* **Asynchronous & High Performance**: Full-stack async processing powered by Python type hints and FastAPI (Async/Await).
* **Distributed Unique IDs**: Primary keys uniformly use **Snowflake algorithm**, which is time-ordered and high-performance, automatically resolving frontend `BigInt` precision loss issues.
* **Elegant Authentication**:
  * Supports both **OAuth2 form login (for Swagger UI)** and **JSON login (for SPA apps)**.
  * Built-in **Redis token blacklist** mechanism enables true backend "logout".
* **Standard RBAC Model**: Permission system based on User–Role–Menu hierarchy, supporting fine-grained button-level permission checks.
* **Unified Response Format**: All APIs follow a consistent structure: `code`, `message`, `data`.
* **Automatic CamelCase Conversion**: Backend uses PEP8-compliant `snake_case`; APIs are automatically converted to frontend-friendly `camelCase`.

## 🛠️ Tech Stack

- **Backend**
  - FastAPI
  - SQLAlchemy 2.0
  - PostgreSQL
  - Redis
  - Alembic

- **Frontend**
  - Vue3
  - Vite
  - Naive UI
  - TypeScript
  - UnoCSS

- **Mobile**
  - Vue3
  - UniAPP

## 📁 Project Structure

```text
hohu-admin/
├── app/
│   ├── core/              # Core framework configs (Security, JWT, Redis, Config)
│   ├── db/                # Database connection & base model
│   │
│   ├── modules/           # 🧩 Modular directory
│   │   ├── auth/          # Auth module (login, token refresh)
│   │   ├── system/        # System management (User, Role, Menu, Dict)
│   │   │   ├── api/       # System APIs
│   │   │   ├── crud/      # Business logic
│   │   │   ├── models/    # Database models
│   │   │   └── schemas/   # Pydantic schemas
│   │   │
│   │   └── business/      # 🚀 Placeholder for custom business modules
│   │       ├── __init__.py
│   │       ├── api/       # Your custom APIs
│   │       └── models/    # Your custom models
│   │
│   └── main.py            # Aggregates all module routes
├── scripts/               # Data initialization scripts
├── alembic/               # Database migration scripts
└── .env                   # Environment variables
```

## 🚀 Quick Start

### Using HoHu CLI (Recommended)

**[HoHu CLI](https://github.com/aihohu/hohu-cli)** is a modern command-line tool tailored for the `hohu-admin` ecosystem. It integrates project scaffolding, automated environment setup, and multi-language switching to boost developer productivity.

1. **Install CLI**  
   Install globally using `uv` (recommended) or `pip`:

   ```bash
   # Using uv
   uv tool install hohu
   
   # Or using pip
   pip install hohu
   ```

2. **Create a New Project**
   ```bash
   hohu admin create my-project
   ```

3. **Initialize Environment**
   ```bash
   hohu admin init
   ```

4. **Run the Project**
   ```bash
   hohu admin dev
   ```

---

### Manual Setup

#### 1. Prerequisites

Ensure you have `uv`, Python 3.10+, PostgreSQL, and Redis installed.

#### 2. Install Dependencies

```bash
uv sync
```

Activate the virtual environment:

```bash
source .venv/bin/activate
```

#### 3. Configure Environment Variables

Copy `.env.example` to `.env` and configure your database and Redis connections:

```env
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/hohu_admin
REDIS_URL=redis://localhost:6379/0
SECRET_KEY=your-super-secret-key
```

#### 4. Database Migration & Initialization

```bash
# Run migrations
alembic upgrade head

# Seed initial data
python scripts/init_db.py
```

#### 5. Start the Server

```bash
fastapi dev app/main.py
```

Visit the interactive API docs at: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

---

## 📝 API Conventions

### Unified Response Format

```json
{
  "code": 200,
  "msg": "success",
  "data": { ... }
}
```

### ID Handling

Because Snowflake IDs are used, all primary keys (e.g., `user_id`) are **automatically serialized as strings** in JSON responses to prevent precision loss during `JSON.parse()` in the frontend.

---

## 📝 Development Conventions

### Field Naming & Frontend-Backend Integration

#### 1. Naming Standards

- **Backend (Python/SQLAlchemy/Pydantic)**: Use `snake_case` exclusively (e.g., `i18n_key`).
- **Frontend (JSON/JavaScript)**: Use `camelCase` exclusively (e.g., `i18nKey`).

#### 2. Automatic Conversion Mechanism

The project uses Pydantic’s `alias_generator` for seamless conversion. Enable it in your base model config:

```python
model_config = ConfigDict(
    alias_generator=to_camel,  # Converts snake_case → camelCase automatically
    populate_by_name=True,     # Allows assignment via either alias or original name
    from_attributes=True       # Enables direct conversion from ORM models
)
```

#### 3. Common Pitfall: Special Abbreviations (e.g., i18n)

##### Problem

Pydantic’s default `to_camel` treats letters after digits as new words:

- **Expected**: `i18n_key` → `i18nKey`
- **Actual**: `i18n_key` → `i18NKey` (note the uppercase **N**)

##### Solution

For such edge cases, **manually specify the alias** using `Field(alias="...")` to override auto-generation.

##### Correct Example

```python
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

class MenuSchema(BaseModel):
    parent_id: int | None = None  # Auto → parentId
    # Manually define alias to ensure correct i18nKey
    i18n_key: str | None = Field(None, alias="i18nKey")

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True
    )
```

#### 4. Data Flow Summary

| **Scenario**             | **Usage**                  | **Field Name (Example)**     |
| ------------------------ | -------------------------- | ---------------------------- |
| **Frontend → Backend**   | JSON Request Body          | `{ "i18nKey": "..." }`       |
| **Backend Internal**     | `menu.i18n_key`            | Use `snake_case`             |
| **Save to DB**           | `menu.model_dump()`        | Outputs `i18n_key` (DB-safe) |
| **Backend → Frontend**   | `ResponseModel(data=menu)` | Auto → `i18nKey`             |

> **Note**: When calling `.model_dump()`, **do not** use `by_alias=True` unless for debugging—doing so may output camelCase fields and cause database write failures.

---

## 🛠️ Custom Module Development Guide

### How to Add a New Module?

1. Create a new folder under `app/modules/`.
2. Define `models.py` (SQLAlchemy entities).
3. Define `schemas.py` (Pydantic models; enable `alias_generator=to_camel`).
4. Implement APIs in `api.py`, protected by `get_current_user` for auth.
5. Mount the router in `app/main.py`.