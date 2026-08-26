# AI Management MVP Phase 4 Release Closure

> Status: ✅ Phase 4 released (2026-08-24)
>
> Sole requirement baseline: [`2026-08-14-ai-management-mvp-closure.md`](./2026-08-14-ai-management-mvp-closure.md). This short spec only records the Phase 4 TDD slices and must not introduce a competing contract.

## 1. Scope

Phase 4 closes the release-only gaps left after the User, Department, and Role
management agents were completed:

1. persist immutable AI Trace agent and allowlisted target facts;
2. expose tenant-scoped, redacted Trace list/detail APIs and an independent Web page;
3. close HITL reload/resume/download/tool-only projection and conversation deletion;
4. prove the complete identity/data-scope matrix through deterministic browser E2E;
5. keep real-provider smoke in a separate release project that fails when credentials are absent;
6. enforce backend and frontend coverage gates at 70% or greater.

Out of scope remains message edit/regenerate and every capability explicitly deferred by the baseline.

## 2. TDD work packages

### Package 1 — AI Trace foundation

- [x] Red: migration/model tests for nullable `agent_code` and `target_summary`, reliable PreparedAction backfill, legacy unknown behavior, and required indexes.
- [x] Red: write-path tests proving Agent and allowlisted target summaries are frozen without raw arguments, prompts, credentials, or unmasked PII.
- [x] Red: list/detail contract tests for permission, tenant isolation, stable pagination/filtering, grouping, 403/404 semantics, and DTO denylisted fields.
- [x] Green: migration, model, service, API, seed/menu permission, and Web Trace list/detail page.

### Package 2 — projection and lifecycle closure

- [x] Red: tool-only/reload/resume/download tests and revocation tests across conversation, resume, query-cache, owner log, and download.
- [x] Red: conversation deletion tests for atomic expiration of prepared/pending/approved actions, running conflict, rollback, and post-delete no-side-effect confirm.
- [x] Green: shared projection/cache invalidation and durable action terminalization.

### Package 3 — release browser matrix

- [x] Deterministic Playwright coverage for every identity in baseline §7.1, Trace success/reject/failure, current-scope HITL reload/resume/download/tool-only, and destructive denial.
- [x] A separate `e2e:provider` project performs User/Department/Role read and controlled write smoke against a real provider, records trace evidence, and fails closed when release credentials are absent.

### Package 4 — release gates and ship state

- [x] Backend Ruff, static Tool checks, full tests, and coverage ≥70%.
- [x] Frontend format/lint/typecheck, unit coverage ≥70%, build, deterministic E2E, and release provider smoke.
- [x] Security review, deployment documentation, all linked spec evidence, ship date, and baseline release-state update.

## 3. Decisions

1. **Trace targets are frozen allowlisted facts, not reconstructed arguments** — `target_summary` is produced from trusted Tool projection metadata at the operation write/update boundary and never from raw arguments, prompts, or mutable conversation state. **Counterexample**: Serializing `frozen_args` into a Trace row would turn an audit feature into a credential and PII exfiltration path. **Regression**: Schema, service, API, and serialization tests assert denylisted names and sentinel secrets never appear.

2. **Trace authorization is independent from owner history projection** — Global Trace list/detail requires `ai:trace:view` or an enabled `R_SUPER`, applies tenant scope, and returns only the audit DTO allowlist. **Counterexample**: Reusing conversation history could reveal message content to an auditor or hide tenant-wide failures that the auditor must inspect. **Regression**: Auditor-without-chat, ordinary owner, disabled-role, and cross-tenant API tests cover distinct 403/404 surfaces.

3. **Conversation deletion terminalizes durable actions before deletion** — The API locks the conversation and its actions, first recovers an expired running lease to a failed terminal state, rejects any live running action, then atomically expires prepared/pending/approved actions with `AI_CONVERSATION_DELETED`, clears leases, and deletes the conversation. **Counterexample**: Only checking for pending actions leaves an orphan that can execute after its conversation disappears, while treating an abandoned lease as live can permanently prevent cleanup. **Regression**: Database tests cover all nonterminal states, expired/live running leases, rollback, and confirm-after-delete zero side effects.

4. **Deterministic and real-provider browser suites are separate release signals** — PR E2E remains offline and reproducible; real-provider smoke is an explicit release project whose missing credentials fail instead of skip. **Counterexample**: Treating route fixtures as provider evidence proves UI wiring but not model routing or hardened egress. **Regression**: Playwright configuration and command-contract tests assert project separation, credential failure, trace evidence, and no route fixture in provider smoke.

5. **Coverage measures product behavior, not workspace framework plumbing** — `test:coverage` keeps global statement/branch/function/line thresholds ≥70% after transparently excluding shared workspace packages, generated routing, layout shell, request transport, and non-AI framework stores; System/AI API wrappers, AI state and management views remain measured. **Counterexample**: Excluding a low-coverage Phase 4 feature file or lowering one threshold would make the release gate cosmetic. **Regression**: Coverage configuration tests pin the fixed exclusions and ≥70% thresholds.

6. **Management PreparedActions freeze the Phase 3 authorization graph** — Department and Role writes accept only their canonical Phase 3 snapshots and convert their complete direct and indirect subject sets into durable refs before confirmation. **Counterexample**: A User-only subject builder can make a valid real-provider Department write expire before the confirmation drawer or omit the Role/User authorization impact from replay checks. **Regression**: `tests/modules/ai/test_prepared_action_service.py` covers all seven Department/Role write contracts and `tests/e2e/provider-smoke.spec.ts` proves the real-provider path.

7. **Soft deletion fences every user-facing message write** — Message-addressed write paths reload the owning conversation and treat `deleted_at IS NOT NULL` as not found before mutating message state or append-only history. **Counterexample**: Routing feedback that validates only message activity and ownership can still write after the conversation has disappeared from all reads. **Regression**: The routing-feedback integration test asserts 404 plus unchanged message state and no feedback row.

## 4. Release evidence

- Backend: Ruff check and format check passed; 2,284 tests passed with 76.59% coverage; all 31 registered Tools passed 12 static checks.
- Frontend: format, lint (0 errors), typecheck, and production build passed; 44 Vitest files / 179 tests passed with statements 79.43%, branches 70.29%, functions 71.96%, and lines 83.06%.
- Browser: deterministic Chrome project passed 28/28 tests, including all 11 baseline identities and the projection/Trace cases. Interactive verification confirmed the AI Trace menu, filters, list, and redacted detail.
- Provider: the isolated DeepSeek release project passed 1/1 in 33.1 seconds and recorded provider/model plus actual Agent/Tool evidence read back from each Trace detail for User, Department, and Role read plus controlled write paths. It also removes its conversation and created business objects. A run without the six required variables failed before execution as required.
- Review: no unresolved Phase 4 correctness or security findings remain. Existing 32 backend SQLAlchemy warnings and 30 frontend lint warnings are outside the Phase 4 changed paths and are non-blocking.
