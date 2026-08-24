# Dept Agent scoped management slice

> Status: Phase 3 complete
> Date: 2026-08-24
> Requirement baseline: [`AI management MVP closure`](./2026-08-14-ai-management-mvp-closure.md) §4.2 and §5
> Scope: Phase 3 Department Agent slice

## 1. Goal and boundary

This slice makes `dept_mgmt` the hierarchy and indirect-authorization proof for the
MVP. The page API and AI tools must use one department selector and one write
policy, so a caller cannot bypass data scope, tree-move authorization, or
authorization-impact analysis by changing entry points.

This package covers:

- `system:dept:move` and the independent page move API;
- scoped traditional reads and `dept.count/list/lookup`;
- scoped `dept.create/update/move` with high-risk HITL and dry-run;
- status and hierarchy changes that indirectly change effective authorization;
- the global role → department → user lock protocol and post-lock revalidation.

This package does not add an AI department-delete Tool or a new Web tree-move
interaction, and it does not include Phase 4 release work. The existing Web edit
form receives the compatibility change needed to stop sending structure fields
through the strict base-update contract. AI department delete remains deferred;
the traditional single and batch delete paths are hardened by the Phase 3
destructive policy. Prompt, i18n, Tool Result, published-Agent inventory, and
fresh/upgrade permission closure are part of the completed Phase 3 integration.

## 2. Public contracts

### 2.1 Permission and page API

`system:dept:move` is a separate function permission. Fresh/sync menu data must
contain it without changing the meaning of `system:dept:edit`. Fresh
`R_SUPER` receives the explicit department Tool permissions it needs; the
additive upgrade migration grants the new move permission only to `R_SUPER`.
Historical roles with `system:dept:edit` are not backfilled because that would
erase the new separation; delegated administrators require an explicit grant.

| API | Required permissions | Contract |
|---|---|---|
| `GET /system/dept/tree` | `system:dept:list` | Scoped local-root tree |
| `GET /system/dept/tree-option` | `system:dept:list` | Scoped enabled local-root options |
| `GET /system/dept/tree-list` | `system:dept:list` | Scoped local-root tree page |
| `GET /system/dept/list` | `system:dept:list` | Scoped stable page |
| `GET /system/dept/{dept_id}` | `system:dept:list` | Out-of-scope ID is the same 404 surface as missing |
| `POST /system/dept/add` | `system:dept:add + system:dept:list` | Shared create policy |
| `PUT /system/dept/{dept_id}` | `system:dept:edit + system:dept:list` | Shared non-structural update policy |
| `PUT /system/dept/{dept_id}/move` | `system:dept:move + system:dept:list` | Body is exactly `{newParentId: string \| null}` |

`DeptUpdate` accepts only `deptName`, `orderNum`, `leader`, `phone`, `email`, and
`status`, forbids extra fields, and requires at least one provided field. It must
reject `parentId` and `ancestors`; structure changes use only the move API.
`DeptMove` accepts a canonical positive Snowflake string or explicit `null`, and
forbids numbers, zero, leading zeroes, booleans, and extra fields.

### 2.2 Shared scoped selector

The traditional API and AI tools share a selector parameterized by the live
`DataScopeResolution`:

- `accessible_dept_ids=None` means the tenant-wide set; an empty set means no
  visible departments.
- Count, page totals, rows, lookup match counts, paths, and projection subject
  references are all computed from the same visible set.
- A partial tree never fetches or returns hidden ancestor names. A visible node
  whose parent is hidden is projected as a local root.
- Lookup searches visible candidates by normalized name or visible path, returns
  at most `1..20` rows in stable path/ID order, and reports all scoped match
  contributors in its immutable result projection. It never uses a hidden same-
  name candidate to create ambiguity or reveal existence.
- Department management lookup includes visible enabled and disabled targets so
  an administrator can re-enable a disabled department. User-assignment lookup
  remains enabled-only and cannot assign users to a disabled department.

### 2.3 AI Tool declarations

| Tool | Required permissions | Contract |
|---|---|---|
| `dept.count/list/lookup` | `system:dept:list` | Low-risk readonly scoped selector |
| `dept.create` | `system:dept:add + system:dept:list` | High-risk, always HITL, dry-run |
| `dept.update` | `system:dept:edit + system:dept:list` | High-risk, always HITL, dry-run; no structure fields |
| `dept.move` | `system:dept:move + system:dept:list` | High-risk, always HITL, dry-run |

Write tools accept stable IDs, never resolve the execution target from a name,
and call the same Service/Policy as the page API. Dry-run freezes normalized
execution arguments and a server-owned business snapshot. Approved execution
requires that snapshot and maps any authorization or target drift to
`AI_PREPARED_ACTION_SNAPSHOT_STALE` before a business write.

## 3. Write policy

### 3.1 Create

- A non-root parent must exist, be enabled, and be in the actor's writable
  department scope. Same-level name uniqueness and maximum depth remain atomic.
- `parent_id=null` creates a tenant root and is restricted to a current super
  administrator.
- A leader or other user reference, when present, must resolve uniquely inside
  the actor's user scope during preview; the stable user ID and display value are
  frozen for approval.

### 3.2 Non-structural update and status impact

- The target must be writable and any referenced user must be in user scope.
- Name/order/contact-only updates do not skip ordinary scope and lock checks, but
  do not manufacture an authorization-impact bypass.
- A status change builds both the current and candidate status facts, discovers
  every role and principal whose `CUSTOM`, `DEPT`, `DEPT_AND_SUB`, or future
  resolver contribution can change, and batch-materializes effective permission,
  menu, Agent, department, and user authorization before and after the change.
- A delegated actor may proceed only when every affected principal is in the
  actor's user scope and every principal's before and after materialized
  department/user sets are subsets of the actor's `GrantAuthority`. Otherwise it
  fails with `AI_DEPT_STATUS_AUTHZ_IMPACT_OUT_OF_SCOPE` and performs no write.

### 3.3 Move and hierarchy impact

- The source, old parent, new parent, and complete source subtree must be in the
  writable department scope for a delegated actor.
- Moving to the tenant root, moving a scope root, or changing any hidden node is
  restricted to a current super administrator.
- Self-parenting, moving below a descendant, unchanged parent, missing target,
  cross-tenant targets, and maximum-depth overflow fail before mutation.
- The candidate tree is materialized without changing ORM facts. Every principal
  whose effective authorization can change through the old or candidate parent
  chain is analyzed in bulk. All affected users must be in actor user scope and
  each before/after materialized department/user set must be within actor
  authority; otherwise fail with `AI_DEPT_MOVE_AUTHZ_IMPACT_OUT_OF_SCOPE`.
- The frozen snapshot contains source/old-parent/new-parent IDs, both parent
  chains, the ordered complete subtree with paths, affected role/principal IDs,
  and canonical before/after authorization hashes.

## 4. Locking and transaction protocol

Every page, AI, import, or background writer that changes department structure or
status uses the same protocol:

1. Pre-read the full possible role, department, and user dependency sets for both
   the current and candidate facts.
2. Acquire all role rows by ascending ID, then all department rows by ascending
   ID, then actor/target/affected user rows by ascending ID through
   `authorization_lock_service.lock_targets()`.
3. Reload actor roles, department status/parents/ancestors/subtree, role custom
   departments, role members, user departments, menus, and active Role-Agent
   bindings. Rebuild the before/after materialized authorization snapshots.
4. If any dependency, ordered set, parent chain, member set, authority version,
   or hash differs—or a new lock target is discovered—fail with
   `AUTHORIZATION_SNAPSHOT_STALE` for a page call or
   `AI_PREPARED_ACTION_SNAPSHOT_STALE` for approved execution. Do not acquire the
   newly discovered lower-order row while holding a later-order lock.
5. Write only after the second Policy pass. Service code never commits; the API
   or Gateway owns the transaction.

The actor row is always part of the user lock set. Runtime relationship writers
must lock their parent aggregate row, preventing a membership phantom after the
snapshot check.

## 5. TDD acceptance and status

The Red packages proved the implementation requirements in these layers:

- permission/schema/route contracts, including the independent move endpoint;
- fresh/upgrade `R_SUPER` mapping and no implicit edit → move expansion;
- scoped count/list/lookup without hidden ancestor or same-name leakage;
- create/update/move Tool metadata and shared Service delegation;
- status and move indirect-authorization rejection with zero mutation;
- complete role → department → user lock discovery and post-lock stale rejection.

✅ Phase 3 Dept slice completed (2026-08-24): the distinct move permission and
independent page endpoint, strict update/move schemas, authenticated scoped reads,
shared selector, and shared create/update/move Service Policy are Green. The
selector treats an empty scope as empty, projects partial trees as local roots,
uses a missing-object 404 for hidden IDs, and freezes only scoped lookup
contributors. Status and hierarchy changes materialize every affected user's
before/after authorization, expose the affected-user count in HITL, and execute
only after the role → department → user lock-time snapshot remains unchanged.

The review pass additionally rejects explicit `null` for non-null update fields,
keeps nullable leader/contact fields clearable, synchronizes the Web update DTO so
`parentId` cannot reach the strict base-update endpoint, and makes traditional
single/batch delete require both current `R_SUPER` membership and the exact
original permission. Reference checks and aggregate locks prevent cascade-based
authorization changes, and any batch rejection leaves all targets unchanged.

Fresh install and additive upgrade paths explicitly grant `R_SUPER` every
published Department Tool permission while preserving existing Agent enabled and
Role-Agent binding state. The published prompt, localized confirmation/result
fields, stable error codes, and Agent inventory include the complete Department
slice. AI department delete remains intentionally deferred.

The closure review additionally makes ancestor matching segment-exact, resolves
leader references uniquely inside the actor's user scope, includes affected Roles
without members in status-impact snapshots, and binds list/null HITL arguments to
safe scalar presentation values without changing frozen execution arguments.

Verification evidence: the complete backend suite passes 2252 tests with 32
existing warnings and 76% total coverage; Ruff check/format and the 31-Tool / 12
static-check gates pass. Web lint has 0 errors and 30 existing warnings; format,
typecheck, 33 files / 128 tests, and the production build pass. Phase 4 owns the
remaining browser release matrix and global Web coverage gate.

## 6. Decisions

1. **Department structure has a distinct permission and endpoint** — Editing
   descriptive fields must not imply authority to rewrite the organization tree.
   **反例**: accepting `parentId` in the base update API bypasses move
   scope and hierarchy-impact analysis. **回归**:
   `tests/modules/system/test_dept_phase3_contracts.py` and
   `tests/modules/system/test_dept_move_permission_migration.py`.

2. **All department reads share one scoped selector** — Tree shape, lookup match
   count, list total, and replay lineage must describe the same currently visible
   set. **反例**: a scoped list paired with an unscoped count or hidden
   ancestor path leaks organization metadata. **回归**:
   `tests/modules/ai/test_dept_phase3_tools.py` and
   `tests/modules/system/test_dept_phase3_scoped_read.py`.

3. **Status and parent changes are authorization changes** — Resolver inputs can
   change even when no role row changes, so direct object scope is insufficient.
   **反例**: enabling a referenced department or moving a subtree can
   expand an out-of-scope principal's effective access. **回归**:
   `tests/modules/system/test_dept_phase3_authorization.py`.

4. **Department authorization writers use the global aggregate lock order** — A
   complete pre-read followed by role → department → user locks makes page and AI
   snapshots comparable and avoids cross-writer deadlocks. **反例**:
   locking the source department first and discovering a role/member later can
   deadlock or authorize against a phantom. **回归**:
   `tests/modules/system/test_dept_phase3_authorization.py`.

5. **Management lookup must retain disabled recovery targets** — Department
   administrators need a stable ID for a disabled department before they can
   request a status update, while user assignment must still exclude it.
   **反例**: sharing an enabled-only lookup makes a disabled department
   impossible to re-enable through the Department Agent. **回归**:
   `tests/modules/ai/test_dept_phase3_tools.py`.

6. **Hierarchy paths and referenced users are immutable authorization facts** —
   Ancestor queries use complete comma-delimited segments and leader lookup freezes
   a scoped stable user ID before approval. **反例**: a prefix match for department
   `12` also mutates `123`, or a free-form leader label references a hidden user.
   **回归**: `tests/modules/system/test_dept_phase3_authorization.py` and
   `tests/modules/ai/test_dept_phase3_tools.py`.
