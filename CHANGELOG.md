# Changelog

All notable changes to this project will be documented in this file.

## [v0.1.4] (2026-07-03)

### Features

- **Marketplace Phase 1 MVP** — App store with upload / install / uninstall / rating / review workflows. Tenant-scoped install with manifest validation, SHA-256 zip hash, reinstall UPDATE strategy with `retained_tables` protection, and ON CONFLICT dedup for permission detail
- **Lowcode Engine** — JSON-Schema → PostgreSQL schema introspection, comparator (widening-safe / breaking-rejected), migration runner (CREATE / ALTER / DROP), and dynamic CRUD data API on auto-created `app_data_*` tables with Redis-backed contributes cache
- **Lowcode belongs_to Relations** — Auto-expand foreign keys as `<fk>_label` on list (N+1-safe batch dedup), JOIN-based sort on label columns, digit-string Snowflake ID coercion for asyncpg BIGINT binding
- **Multi-Menu Apps** — `manifest.menus` (plural) takes precedence over singular `menu`; demo CRM upgraded to customer + order multi-model
- **Marketplace SSRF Protection** — `SafeHttpClient` with scheme/pattern/IP blocklist, IPv4-mapped IPv6 dual-stack check, redirect-disabled, 1MB body cap, 5s/10s timeout; `SSRFBlockedException` with errorCode
- **Button-Level Permission (End-to-End)** — All system / ai / marketplace write endpoints gated by `require_permissions()`; ownership check on file delete; errorCodes `MISSING_PERMISSION` / `SUPER_ADMIN_ONLY` / `FILE_OWNERSHIP_REQUIRED` for frontend i18n
- **Auth Refresh Token** — Refresh token endpoint + audit middleware username cache to reduce DB lookups per request
- **Scheduler Hardening** — Dedicated scheduler process to avoid duplicate execution across uvicorn workers; per-job `timeout` / `retry` / `next_run_time` / `run_on_enable`; pubsub leak fix
- **Data Scope Demo** — `sys_data_scope_demo` table with seed data (`seed_demo_data_scope.py`) and full RBAC filter chain for documentation / onboarding
- **Department User Management** — Dept users endpoints + `user_require_primary_dept` config-driven validation
- **`LocalNaiveDatetime` Type** — Cross-module Pydantic type for datetime range queries (NDatePicker unix-ms → naive UTC); fixes asyncpg aware/naive TypeError on `TIMESTAMP WITHOUT TIME ZONE` columns

### Bug Fixes

- **RBAC Consistency & Atomicity** — `menu_service.update_menu` switches to incremental button update by permission code (preserves role_menus associations, was delete-rebuild broke CASCADE); `role_service.get_role_menus` returns true leaves and excludes orphan parents (NTree cascade all-select bug)
- **Menu F-Type Button** — `parent_id` chain bug in `sync_menus`
- **Audit Timezone** — Normalize tz-aware datetimes to naive UTC for log queries and cleanup
- **Reverse Proxy IP** — Read real client IP from `X-Forwarded-For` behind reverse proxy

### Improvements

- **Demo Seeds** — `seed_demo_crm.py` upgraded to multi-model (customer + order) demonstrating `belongs_to` relations
- **Documentation** — `docs/button-permission-guide.md`, `docs/data-scope-guide.md`, `docs/MODULE-DEVELOPMENT-GUIDE.md`, `docs/specs/2026-07-01-*.md`
- **Docker** — Pin `uv` to 0.11.26 to avoid repulling on every build
- **Tests** — Add `tests/modules/marketplace/`, `tests/modules/system/test_data_scope_*`, `tests/modules/job/test_job_log_service.py`, `tests/schemas/test_types.py`

## [v0.1.3] (2026-06-11)

### Features

- **Config Excel Export/Import** — Export system configs to Excel file and import from Excel, with button-level permission control
- **Menu Sync Expansion** — Add operation log, login log and monitor menus to `sync_menus` utility
- **User Role Display** — Show role names in user list and add role-based filter support
- **Advanced Filter Operators** — Support `ilike`, `startswith`, `endswith`, `between`, `is_null` and other operators in query builder

### Bug Fixes

- **User Edit Password** — Allow empty password on user edit to avoid forced password reset
- **Config Query Helpers** — Add query helper functions for config module

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
