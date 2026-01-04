# 🥞 Pancake Admin

<div align="center">
  <span>English | <a href="./README.md">中文</a></span>
</div>

[![license](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)
[![github stars](https://img.shields.io/github/stars/aihohu/hohu-admin)](https://github.com/aihohu/hohu-admin)
[![github forks](https://img.shields.io/github/forks/aihohu/hohu-admin)](https://github.com/aihohu/hohu-admin)
---

**Pancake** is a modern, high-performance, modular backend admin template built with **Python** and **FastAPI**. It uses **SQLAlchemy 2.0 (async)** as its core ORM and is designed specifically for modern frontend-backend decoupled architectures. Out of the box, it provides a complete production-grade backend foundation—including user authentication, role-based access control (RBAC), distributed ID generation, database migrations, logging, monitoring, and integrated API documentation.

In an era where AI applications are rapidly going to market, **Pancake** empowers developers to escape repetitive infrastructure work and focus on business innovation and intelligent integration. Whether you're building a quick prototype or a scalable enterprise application, Pancake significantly lowers the technical barrier, shortens development cycles, and enhances code quality and system security—making it easier than ever to embrace the AI era.

## ✨ Key Features

* **Async & High Performance**: Fully asynchronous stack powered by Python type hints and FastAPI (async/await throughout).
* **Distributed Unique IDs**: Primary keys use **Snowflake IDs**—time-ordered, high-performance, and automatically serialized as strings to prevent precision loss in frontend `BigInt` handling.
* **Elegant Authentication**:
  * Supports both **OAuth2 password flow (for Swagger UI)** and **JSON-based login (for SPAs)**.
  * Built-in **Redis token blacklist** for true server-side "logout".
* **Standard RBAC Model**: User–Role–Menu permission system with support for button-level authorization.
* **Unified Response Format**: All APIs follow a consistent structure: `{ code, msg, data }`.
* **Automatic CamelCase Conversion**: Backend uses PEP8 snake_case; API responses are automatically converted to frontend-friendly camelCase.

## 🛠️ Tech Stack

* **Framework**: FastAPI (Python 3.12+)
* **Database**: PostgreSQL 16+
* **ORM**: SQLAlchemy 2.0 (Async)
* **Cache**: Redis
* **Migrations**: Alembic
* **Security**: PyJWT, Passlib (Bcrypt)

## 📁 Project Structure

```text
hohu-admin/
├── app/
│   ├── core/              # Core config (Security, JWT, Redis, Settings)
│   ├── db/                # DB connection & base model
│   │
│   ├── modules/           # 🧩 Modular design
│   │   ├── auth/          # Auth module (login, token refresh)
│   │   ├── system/        # System management (User, Role, Menu, Dict)
│   │   │   ├── api/       # System APIs
│   │   │   ├── crud/      # Business logic
│   │   │   ├── models/    # SQLAlchemy models
│   │   │   └── schemas/   # Pydantic schemas
│   │   │
│   │   └── business/      # 🚀 Placeholder for your custom modules
│   │       ├── __init__.py
│   │       ├── api/       # Your custom APIs
│   │       └── models/    # Your custom models
│   │
│   └── main.py            # Main entry: mounts all routes
├── scripts/               # Data seeding scripts
├── alembic/               # Database migration files
└── .env                   # Environment configuration
```

## 🚀 Quick Start

### 1. Environment Preparation

Ensure that uv, Python 3.10+, PostgreSQL, and Redis are installed.

### 2. Install Dependencies

Install all dependencies:

```bash
uv sync
```

Activate the virtual environment using the following command:

```bash
source .venv/bin/activate
```

### 3. Configure Environment

Copy `.env.example` to `.env` and update your database and Redis connections:

```env
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/pancake
REDIS_URL=redis://localhost:6379/0
SECRET_KEY=your-super-secret-key
```

### 4. Run Migrations & Initialize Data

```bash
# Apply database migrations
alembic upgrade head

# Seed initial data (creates admin user)
python scripts/seed_data.py
```

### 5. Start the Server

```bash
fastapi dev app/main.py
```

Visit the interactive API docs at: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

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

All primary keys (e.g., `user_id`) are generated using Snowflake and **automatically serialized as strings** in JSON responses to avoid precision loss when parsed by JavaScript (`Number.MAX_SAFE_INTEGER` limitation).

## 🛠️ Custom Module Development Guide

### How to Add a New Module?

1. Create a new folder under `app/modules/`.
2. Define your `models.py` (SQLAlchemy entities).
3. Define `schemas.py` (Pydantic models; enable `alias_generator=to_camel` for camelCase).
4. Implement endpoints in `api.py`, protected by `get_current_user` or RBAC decorators.
5. Mount the router in `app/main.py`.

---

> 💡 **Tip**: Keep your business logic separate from the `system/` module. Use `business/` as your starting point for domain-specific features.