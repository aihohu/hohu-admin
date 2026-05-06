# HoHu Admin

<p align="center">
  <b>AI-driven modern full-stack admin platform · Backend</b>
</p>

<p align="center">
  <a href="https://show.hohu.org">Demo</a> ·
  <a href="https://github.com/aihohu/hohu-admin-web">Frontend</a> ·
  <a href="https://github.com/aihohu/hohu-admin-app">Mobile</a> ·
  <a href="https://hohu.org/guide/introduction.html">Docs</a>
</p>

<p align="center">
  <a href="./README.zh_CN.md">简体中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="license" />
  <img src="https://img.shields.io/badge/python-%E2%89%A53.12-3776AB.svg" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-0.127-009688.svg" alt="FastAPI" />
  <img src="https://img.shields.io/badge/SQLAlchemy-2.0-D71F00.svg" alt="SQLAlchemy" />
  <img src="https://img.shields.io/badge/PostgreSQL-17-4169E1.svg" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/Redis-7-DC382D.svg" alt="Redis" />
  <img src="https://img.shields.io/github/stars/aihohu/hohu-admin" alt="GitHub stars" />
  <img src="https://img.shields.io/github/forks/aihohu/hohu-admin" alt="GitHub forks" />
</p>

---

HoHu Admin is a modular backend management platform for decoupled frontend-backend architectures. It provides out-of-the-box infrastructure — authentication, RBAC, Snowflake IDs, scheduled jobs, file management, AI integration, and more — so you can focus on building features, not scaffolding.

## Features

- **Fully Async** — FastAPI + SQLAlchemy 2.0 async ORM end-to-end
- **Snowflake IDs** — Distributed-safe primary keys, auto-serialized as strings for JavaScript
- **RBAC** — User → Role → Menu permission model with button-level access control
- **Dual Login** — OAuth2 form (Swagger UI) + JSON body (SPA) authentication
- **Built-in AI** — Streaming chat via OpenAI-compatible providers with multi-provider management
- **Job Scheduler** — APScheduler-based task management with log tracking
- **File Management** — Upload, storage, and serving with extension/size validation
- **Auto CamelCase** — `snake_case` backend ↔ `camelCase` frontend via Pydantic alias generation
- **Unified Response** — All endpoints follow `{code, msg, data}` envelope with i18n-ready error codes
- **Docker Ready** — Multi-stage Dockerfile + docker-compose with health checks

## Tech Stack

| Layer | Technologies |
|-------|-------------|
| **Framework** | FastAPI, Pydantic v2, Uvicorn |
| **ORM** | SQLAlchemy 2.0 (async), Alembic migrations |
| **Database** | PostgreSQL |
| **Cache** | Redis |
| **Auth** | JWT (HS256), bcrypt, OAuth2 |
| **AI** | OpenAI SDK, pydantic-ai |
| **Tasks** | APScheduler |
| **Linting** | Ruff |

## Ecosystem

HoHu Admin is part of a monorepo with dedicated frontend, mobile, desktop, docs, and CLI projects:

| Project | Description |
|---------|-------------|
| [hohu-admin-web](https://github.com/aihohu/hohu-admin-web) | Vue 3 + Naive UI + TypeScript admin dashboard |
| [hohu-admin-app](https://github.com/aihohu/hohu-admin-app) | Cross-platform mobile app (uni-app + Vue 3) |
| [hohu-admin-desktop](https://github.com/aihohu/hohu-admin-desktop) | Electron desktop application |
| [hohu-admin-docs](https://github.com/aihohu/hohu-admin-docs) | VitePress documentation site |
| [hohu-cli](https://github.com/aihohu/hohu-cli) | CLI scaffolding tool for project setup |

## Project Structure

```
hohu-admin/
├── app/
│   ├── core/                # Config, security, exceptions, response models
│   ├── db/                  # Async engine & session factory
│   ├── middleware/           # Rate limiting
│   ├── modules/
│   │   ├── auth/            # Login, register, JWT, user info, routes
│   │   ├── system/          # User, Role, Menu, Dept, Dict, File
│   │   ├── job/             # Scheduled tasks & execution logs
│   │   └── ai/              # Chat, conversations, providers, agents
│   ├── utils/               # Pagination, masking, helpers
│   └── main.py              # App entry point
├── alembic/                 # Database migrations
├── scripts/                 # Seed & setup scripts
├── tests/                   # Pytest test suite
├── Dockerfile               # Multi-stage production build
└── docker-compose.yml       # One-command deployment
```

## Getting Started

### Prerequisites

- Python >= 3.12
- PostgreSQL
- Redis
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Option 1: Using HoHu CLI (Recommended)

```bash
# Install CLI
uv tool install hohu

# Create and set up project
hohu admin create my-project
cd my-project/hohu-admin
hohu admin init
hohu admin dev
```

### Option 2: Manual Setup

```bash
# Clone the repository
git clone https://github.com/aihohu/hohu-admin.git
cd hohu-admin

# Install dependencies
uv sync

# Configure environment
cp .env.example .env
# Edit .env with your database, Redis, and secret key settings

# Run migrations and seed data
alembic upgrade head
python scripts/init_db.py

# Start dev server
fastapi dev app/main.py
```

The interactive API docs will be available at `http://127.0.0.1:8000/docs`.

### Option 3: Docker

```bash
docker compose up -d
```

Configure via `.env` file and `UVICORN_WORKERS` environment variable (default: 4).

## API Modules

| Module | Prefix | Features |
|--------|--------|----------|
| **Auth** | `/auth` | Login, register, user info, dynamic routes |
| **User** | `/system/user` | CRUD, batch delete, role assignment |
| **Role** | `/system/role` | CRUD, menu permissions |
| **Menu** | `/system/menu` | Tree CRUD, route management |
| **Department** | `/system/dept` | Tree CRUD |
| **Dict** | `/system/dict-type`, `/system/dict-data` | Type/data CRUD |
| **File** | `/system/file` | Upload, batch upload, CRUD |
| **Job** | `/system/job` | CRUD, enable/disable, manual trigger |
| **Job Log** | `/system/job-log` | Paginated logs, cleanup |
| **AI Chat** | `/ai/chat` | Streaming (SSE) & sync chat |
| **AI Conversation** | `/ai/conversation` | CRUD, message history |
| **AI Provider** | `/ai/provider` | Multi-provider management, model list, connectivity test |

## Response Format

All API responses follow a unified envelope:

```json
// Success
{"code": 200, "msg": "success", "data": {...}}

// Paginated
{"code": 200, "msg": "success", "data": {"records": [...], "total": 100, "current": 1, "size": 10}}

// Error (with i18n-ready errorCode)
{"code": 401, "msg": "Token 无效或已过期", "data": null, "errorCode": "TOKEN_EXPIRED"}
```

## Development

### Adding a New Module

1. Create `app/modules/<name>/` with `api/`, `service/`, `models/`, `schemas/`
2. Define SQLAlchemy model with `Mapped[T]` and Snowflake PK (`default=next_id`)
3. Create Pydantic schemas with `alias_generator=to_camel`
4. Implement service logic (raise domain exceptions, never commit)
5. Wire API endpoints (call service, `await db.commit()`, return `ResponseModel`)
6. Register router in `app/main.py`
7. Run `alembic revision --autogenerate -m "add <name>" && alembic upgrade head`

### Code Quality

```bash
ruff check . && ruff format .   # Lint + format
pytest                           # Run tests
```

## License

[MIT](./LICENSE) &copy; HoHux
