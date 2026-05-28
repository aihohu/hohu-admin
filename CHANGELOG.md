# Changelog

All notable changes to this project will be documented in this file.

## [v0.1.2] (2026-05-28)

### Features

- **AI Model Management** — Separate `ai_model` table with capabilities (`text`, `vision`, `image-gen`, `video`, `audio`, `embedding`), per-model base URL, and sort order
- **Model CRUD API** — Nested model endpoints under provider (`/{provider_id}/models`) for add/update/delete
- **Image Vision Support** — Upload images in AI chat, auto-convert local images to base64 for LLM providers
- **Structured Message Parts** — `ai_message.parts` JSON column for multi-modal message content

### Bug Fixes

- **Path Traversal Protection** — Restrict image file reads to upload directory only
- **Async Image Conversion** — Move base64 encoding to thread pool to avoid blocking event loop
- **Audit Middleware** — Skip JSON body parsing for multipart/form-data requests
- **Conversation Model Default** — Fix NOT NULL violation when creating conversation without model selection

### Improvements

- **Model Resolution Refactor** — Extract `_find_model()` and `_build_model_instance()` to eliminate duplicate code, remove old `provider:model` format compatibility
- **Model Fallback** — Fallback query filters by `text` capability to avoid routing to image-gen models
- **Frontend Model Selector** — Model cards with capability tags, i18n labels, required validation on name and capabilities
- **GIN Index** — `ai_model.capabilities` JSONB column with GIN index for efficient capability queries
- **Data Migration** — Alembic migration moves `config.models` to `ai_model` rows, cleans up old config

## [v0.1.1] (2026-05-06)

### Features

- **AI Chat Module** — Streaming & sync chat with multi-provider management and Pydantic AI agents
- **Scheduled Job Module** — APScheduler-based task management with job log tracking
- **File Upload Module** — Upload, batch upload, and file serving with size/extension validation
- **Data Permission** — Role-based row-level filtering via `data_scope`
- **OAuth2 Token Endpoint** — `/auth/token` for Swagger UI authentication
- **Auto File URLs** — `SERVER_URL` config for generating full file access URLs
- **Auth Error Codes** — `errorCode` field on auth exceptions (`INVALID_CREDENTIALS`, `TOKEN_EXPIRED`, `ACCOUNT_DISABLED`, `UNSUPPORTED_LOGIN_TYPE`) for frontend i18n mapping

### Bug Fixes

- **Standardize 401 Response** — Add `HTTPException` global handler to unify response format, preventing frontend stuck on auth failure
- **Hard Delete Files** — Change file deletion from soft delete to hard delete with disk cleanup

### Improvements

- Add pre-commit hooks

## [v0.1.0] (2026-04-16)

### Features

- **RBAC Permission System** — User → Role → Menu hierarchy with button-level access control
- **Department Management** — Tree structure with search/query
- **Dictionary Management** — Dict type & dict data CRUD
- **Rate Limiting** — IP-based API rate limiting middleware
- **Snowflake ID** — Distributed-safe primary keys with configurable `WORKER_ID`
- **Auth Module** — JWT (HS256) authentication with login, register, user info, and dynamic routes
- **Docker Support** — Multi-stage Dockerfile with GHCR publishing workflow
- **Database Migrations** — Alembic with async PostgreSQL support
- **CI/CD** — GitHub Actions for Docker image publishing and PyPI publishing

### Bug Fixes

- Fix super administrator menu permissions
- Fix Windows CRLF formatting issues
- Fix database configuration and init script issues
- Handle duplicate tables and existing seed data during init

### Improvements

- Unified response format with `ResponseModel`
- Auto `snake_case` ↔ `camelCase` conversion via Pydantic
- Optimized exception handling with domain-specific exception hierarchy
- Enhanced parameter validation
