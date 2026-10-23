# Changelog

All notable changes to this project will be documented in this file.

## [v0.1.6]

### Compatibility and Security Fixes

- Scope AI username uniqueness and user counts to the current tenant and permitted rows. HTTP and AI user creation share the same transaction-neutral business orchestration and retain role, department, and default-password checks.
- Commit supported AI database writes, prepared-action success, and required success audit facts in one transaction. Reconcile lost commit acknowledgements and interrupted result delivery from durable facts without repeating the business operation or reporting a committed write as failed.
- Preserve unknown legacy outcomes for evidence-based reconciliation. Guard cleanup, cancellation, deletion, read-only calls, and history projections so they cannot erase unresolved outcomes or expose results outside current permissions.
- Keep file preparation and preview handoff consistent with database completion, with rollback cleanup and post-commit deletion handled at their respective transaction boundaries.
- Render cancellation and preflight rejection accurately. Proven preflight rejection releases the conversation for a new request without retrying the rejected action; ambiguous historical records still require evidence-backed reconciliation.

### Release and Maintenance

- Apply CI to version branches and require same-version Backend/Web product combinations. Preserve existing fixes to footer link attribution.
- Protect complete-version container references against overwrites. Recover delivery only from the first successful build in the same workflow run, and preserve the existing series and `latest` aliases.
- Add the `release.json` v1 contract, strict offline and online provenance validation, and immutable attachment publication with acknowledgement-loss recovery. Component announcements alone do not establish product qualification; use the validated product manifest and its evidence.

### Compatibility and Upgrade Notes

- Pair Backend 0.1.6 with Web 0.1.6. CLI 0.1.16 supports the documented manual digest-pinning procedure; it does not validate `release.json`, terminate old writers, or pause scheduled jobs automatically. Mobile and desktop clients are not released as part of this product version.
- The candidate installation, original 0.1.5 upgrade, and recovery path has been exercised on `linux/amd64`. Final published artifacts require their own combination evidence before deployment; a built `linux/arm64` image is not a claim of verified product support.
- This patch adds no database migration; the current migration head remains `d01010100101`. Preserve database, public/private files, configuration, encryption keys, and current Redis revocation facts. Follow the [fixed deployment and recovery guide](https://github.com/aihohu/hohu-admin-docs/blob/86691fb578ed17768796d8a7c6b95957e226043c/docs/guide/operations/upgrade.md).
- Before migration, terminate all old writers, fence stale database credentials, and keep the target scheduler paused until acceptance. Unknown AI outcomes must not be replayed. AI user creation requires an explicitly configured valid default password.
- Restoring an old Redis snapshot or incrementing a restored authorization version can revive revoked tokens. Preserve complete current revocation facts or apply the guide's proven complete-watermark invalidation procedure while access remains closed; keep the original shared encryption key. Recovery to 0.1.5 must keep affected AI operations disabled (`AI_MODULE_ENABLED=false`).
- Production installation must use the verified manifest's complete image digests. Series and `latest` tags are not promoted by this release, and source creation from `main` is not a reproducible fixed-product installation.

## [v0.1.5]

### Features

- **Hosted multi-tenancy** — Add tenant creation, atomic initialization, enable/disable lifecycle, trusted login resolution, and tenant-scoped users, roles, departments, files, settings, jobs, and AI resources. Single-tenant mode remains the default; hosted mode supports multiple initialized, active tenants.
- **System administration** — Manage tenants, system Agents, Providers, and tenant model policies through ordinary system-administrator sessions. Global administration requires an enabled `R_SUPER` role in the default tenant and records authorization and completion audit context.
- **AI Agents and routing** — Add domain-agent administration, Role-Agent grants, supervisor routing, routing feedback, and tenant-scoped trace and operation-log queries. Role-Agent configuration exposes tool permission requirements and grant status.
- **Governed AI business tools** — Add user import/export and user, department, role, membership, and job-schedule tools. Tool execution shares application authorization and data-scope services rather than granting an independent AI bypass.
- **Durable confirmations** — Persist prepared actions, frozen arguments, target identities, and confirmation presentation. Approvals recheck current authorization and prevent repeated execution; restored confirmation streams observe persisted outcomes without approving or replaying tools.
- **Model administration** — Restore model management for system administrators, add batch tenant-model authorization, and support connectivity tests for saved configurations and unsaved Provider/model forms without persisting draft settings.
- **Grouped system settings** — Separate tenant settings in `sys_setting` from custom parameters in `sys_config`; add grouped validation, revision conflict checks, safe secret projections, and shared runtime policies for branding, language, accounts, uploads, and access controls.
- **Email account services** — Add system-level SMTP configuration and testing, configurable registration with email verification, password recovery, and authenticated recovery-email binding. Verification codes are tenant- and purpose-bound, expire, and are consumed once; successful password resets revoke old sessions.
- **Monitoring** — Add AI and tenant-rollout metrics, monitoring rules, and orphan job-log recovery, with deployment and maintenance guidance.

### Security and Bug Fixes

- **Current authorization checks** — Enforce tenant, ownership, Agent/model grants, explicit tool permissions, and business data scope during execution, confirmation approval, history projection, and downloads. Tenant rollback and security-version changes invalidate affected authority.
- **Confirmation targets** — Show complete batch-delete targets and refuse lists that exceed confirmation-field limits; bind cron confirmations and historical results to the same authorized tenant job. Keep Snowflake IDs as strings in client presentation.
- **AI result and stream recovery** — Preserve safe deletion receipts after target removal, recover durable confirmations in memory mode without extending expiry, and clean up database connections when AI streams disconnect or are cancelled.
- **User creation and imports** — Respect the tenant's optional-primary-department policy in AI creation, retain role-delegation checks, and apply shared username validation to CSV/Excel imports with original row errors. Flush new users before department assignment.
- **Private files and uploads** — Protect import/export artifacts with tenant and owner checks; validate public image content and untrusted spreadsheets, limit parsing and upload resources, and prevent path traversal and unsafe legacy public access.
- **Provider and secret handling** — Restrict Provider egress by allowed origins/IP ranges, timeouts, response limits, concurrency, and retries. Mask sensitive settings and Provider credentials and keep passwords out of model context and tool results.
- **Transactions and validation** — Commit tenant changes and completion audit records before sending successful responses; preserve authorization aggregates during role/menu changes and return field errors for invalid job schedules.

### Database, Deployment, and Maintenance

- Consolidate unreleased Alembic history while preserving the v0.1.4 release boundary and the current head. Retain default-tenant model grants, menu-parent and login-audit conversion, settings migration, data repairs, and database audit/security-version triggers.
- Unify initialization under `scripts.init_db`, with migration failure stopping seed execution, one seed transaction, safe incremental updates, and explicit first-install administrator credentials. Move development checks and operations tools out of deployment initialization.
- Reorganize menus for fresh installations and remove Marketplace defaults. Preserve existing customized menu structure and grants during incremental synchronization.
- Add hosted release qualification tied to a clean source commit and isolated PostgreSQL/Redis evidence. Keep fast pre-commit checks separate from full acceptance and CI tests.
- Add GitHub-to-Gitee source mirroring, default-branch manual test-image builds, and optional ACR synchronization with matching multi-architecture digests and anonymous full-layer verification. Disable shared-build provenance/SBOM attestation manifests for ACR compatibility.
- Curate public maintenance documents and centralize user, deployment, and extension tutorials in the documentation site. Keep raw ADRs, plans, and acceptance reports outside public manuals.

### Licensing

- Adopt Apache-2.0 as the project license starting with v0.1.5, except where otherwise noted. Previously released versions through v0.1.4 retain their original MIT license.
- Note the MIT history for versions through v0.1.4 in `NOTICE`; include `LICENSE` and `NOTICE` in Python distributions.
- Update contribution guidance and document the transition in `docs/LICENSE-POLICY.md`. Third-party dependencies retain their own licenses.

### Upgrade Notes

- Back up the database, public/private files, deployment configuration, and original encryption keys. Automatic migrations support empty databases and the v0.1.4 boundary `bf244f9a8b76`; older releases must first reach that boundary using their matching code. Squashed development revisions require a separately reviewed conversion or rebuild, not `stamp head`.
- Built-in settings move from `sys_config` to `sys_setting`; custom parameters stay in `sys_config`. Account-service migration disables public registration. Configure and test SMTP before explicitly enabling registration or password recovery; existing email addresses are not automatically trusted recovery addresses.
- Deploy a compatible Web build for tenant administration, model management, grouped settings, account services, and durable confirmations. Components version independently; do not assume that matching numbers or all latest builds establish compatibility.
- System-level tenant/Agent/Provider/model-policy management uses an ordinary default-tenant system-administrator access token plus required audit headers. Legacy platform tokens do not authorize those management APIs; other platform operations retain their own authentication requirements.
- Keep private artifacts outside `UPLOAD_DIR`, using `PRIVATE_UPLOAD_DIR` and `LOCAL_FILE_STORAGE_ROOT`. Review existing files and deny legacy `/uploads/file_storage/**` and public spreadsheet/text download paths at reverse proxies.
- Hosted login remains opt-in through `TENANT_MODE=hosted` and `TENANT_HOSTED_LOGIN_ENABLED`; production hosted deployments require an exact `RELEASE_BUILD_SHA`. Confirm tenant initialization, current grants, and revocation behavior before reopening traffic.
- Marketplace, Contributes, and Lowcode data routes are not registered in the main application. Existing market menus are not automatically deleted; retained code and old menu entries do not mean the capability is available.
- AI write tools still execute through request-time paths. A persisted approval or restored stream does not provide a background queue or guarantee completion after disconnects or restarts. Code rollback does not restore databases or files.

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
