# Role Agent delegated management slice

> Status: Phase 3 complete
> Date: 2026-08-24
> Requirement baseline: [`AI management MVP closure`](./2026-08-14-ai-management-mvp-closure.md) §4.3 and §5

## 1. Boundary

This slice completes the `role_mgmt` vertical path without creating a second
authorization system. Traditional page writers and AI tools share one Role
Delegation Policy built on the Phase 2 `GrantAuthority`, materialized member
authorization, and role → department → user lock protocol.

The slice includes `role.count/list/lookup/create/update/update_menus/update_agents`,
strict immutable role-code handling, complete menu/Agent replacement, member-wide
impact analysis, and lock-time snapshot revalidation. AI role delete remains
deferred; traditional Role delete is handled by the Phase 3 destructive policy.

## 2. Contracts

- Role metadata reads require `system:role:list` and return only ID, code, name,
  status, data scope, `delegable`, and `blockedReasonCode`.
- `RoleUpdate` forbids `roleCode`, extra fields, and empty writes. The Web edit
  form keeps role code read-only and never submits it to the base update API.
- `role.create/update/update_menus/update_agents` are high-risk, always-HITL,
  dry-run tools. Names are lookup/display inputs only; execution uses frozen IDs.
- A normal actor must dominate the old and candidate role definition. Protected
  roles, self-membership, hidden members, or any member whose before/after final
  authorization exceeds the actor fail closed.
- Complete menu replacement derives required ancestors and compares old/new menu
  and permission sets. Complete Agent replacement compares old/new bindings with
  `grantable_agent_ids`, including globally disabled Agents.
- `CUSTOM` is the only data scope that accepts non-empty department assignments.
  Creating or updating any other scope with department IDs fails instead of
  preserving a latent scope that could become active after a later scope change.
- Preview and execution freeze the actor authority, target definition, members,
  before/after materialized authorization, and normalized arguments. Execution
  locks role → department → user once, rebuilds the snapshot, and maps drift to
  the page or AI stale error.

## 3. TDD acceptance and status

✅ Phase 3 Role slice completed (2026-08-24): scoped count/list/lookup and the
shared create/update/menu/Agent aggregate Policy are Green. Page writers and AI
Tools use the same delegated-authority checks, protected-role and self-change
guards, member-wide before/after materialization, stable role → department → user
locks, and lock-time snapshot revalidation. Dry-runs report the complete affected
member count and approved execution rejects any business or authorization drift.

The review pass rejects immutable `roleCode` updates and latent non-`CUSTOM`
department assignments, avoids loading the Tool Registry when the AI module is
disabled, and makes traditional single/batch delete require current `R_SUPER`
membership plus the exact original permission. Role members, child relationships,
and authorization references are checked under aggregate locks; batch failure is
atomic and no cascade may silently change authorization.

Fresh install and additive upgrade paths explicitly grant `R_SUPER` every
published Role Tool permission while preserving deployment-owned Agent state.
Prompt, confirmation/result i18n, stable error mappings, and the published Agent
inventory cover the complete Role slice. AI role delete remains intentionally
deferred.

The closure review additionally makes page reads return only minimal summaries,
reauthorizes full detail/menu/Agent reads and historical write projections against
current delegation facts, locks every finite department materialized before or
after a Role change, and scalarizes list/null confirmation presentation values.

Verification evidence: the complete backend suite passes 2252 tests with 32
existing warnings and 76% total coverage; Ruff check/format and the 31-Tool / 12
static-check gates pass. Web lint has 0 errors and 30 existing warnings; format,
typecheck, 33 files / 128 tests, and the production build pass. Phase 4 owns the
remaining browser release matrix and global Web coverage gate.

## 4. Decisions

1. **Role code is immutable after creation** — Stable codes are authorization and
   integration identifiers, not editable display text. **反例**: accepting
   `roleCode` through the base update API lets a delegated actor rename a role
   into or out of a protected namespace. **回归**:
   `tests/modules/system/test_role_phase3_contracts.py`.

2. **Role reads describe delegation but never grant it** — Minimal summaries may
   explain why a target is blocked, while every write reruns the complete Policy.
   **反例**: treating `delegable=true` from an old list result as an execution
   token bypasses authority and member drift. **回归**:
   `tests/modules/ai/test_role_phase3_tools.py`.

3. **All role aggregates use member-wide locking and revalidation** — A role
   definition change is an authorization change for every member. **反例**:
   locking only the target Role permits a concurrent member insertion to receive
   unreviewed permission, Agent, or scope expansion. **回归**:
   `tests/modules/system/test_role_phase3_authorization.py`.

4. **Only CUSTOM roles may retain department assignments** — Department IDs are
   executable scope facts and cannot remain dormant behind another data-scope
   enum. **反例**: creating a tenant-wide role with hidden custom departments and
   later changing only its scope activates an authorization set that was never
   reviewed together. **回归**:
   `tests/modules/system/test_role_phase3_authorization.py`.

5. **Role read capability is not a persisted delegation capability** — Full Role
   definitions and historical write results rerun the current delegation and member
   boundary, while tenant-wide reads remain minimal summaries. **反例**: retaining
   old menu/Agent details after the actor loses the delegation ceiling turns history
   into an authorization token. **回归**:
   `tests/modules/system/test_role_phase3_contracts.py`,
   `tests/modules/ai/test_role_agent_delegation.py`, and
   `tests/modules/ai/test_result_projection_service.py`.
