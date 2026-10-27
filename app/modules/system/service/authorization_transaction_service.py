"""Complete authorization lock plans, with database-issued transaction proofs.

The owner supplies authorization and commits after successful scope exit. No
existing writer is implicitly migrated by importing this service.
"""

import asyncio
import math
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from itertools import groupby

from sqlalchemy import column, delete, func, insert, select, table, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Insert, Update

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    ResourceKey,
    ResourceKind,
    TenantLock,
    TransactionBudget,
)
from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import (
    AuthorizationPlanSession,
    _order_error,
    _timeout_error,
)
from app.modules.system.field_policy.documents import catalog_digest
from app.modules.system.field_policy.user_fields import CATALOG_REVISION
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.service.authorization_resource_catalog import (
    key_values,
    resource_spec,
)
from app.modules.system.settings_catalog import SETTINGS

_MUTABLE_COLUMNS = {
    ResourceKind.USER: frozenset(
        {
            "user_name",
            "employee_no",
            "nickname",
            "hashed_password",
            "status",
            "auth_version",
            "user_avatar",
            "user_email",
            "email_verified_at",
            "user_phone",
            "user_gender",
        }
    ),
    ResourceKind.ROLE: frozenset(
        {
            "i18n_keys",
            "role_name",
            "role_code",
            "role_desc",
            "data_scope",
            "status",
            "update_by",
        }
    ),
    ResourceKind.DEPT: frozenset(
        {
            "parent_id",
            "ancestors",
            "dept_name",
            "order_num",
            "leader",
            "phone",
            "email",
            "status",
            "update_by",
        }
    ),
    ResourceKind.USER_DEPTS: frozenset({"is_primary"}),
    ResourceKind.IMPORT_BATCH: frozenset(
        {
            "status",
            "started_at",
            "finished_at",
            "success_count",
            "skipped_count",
            "overwritten_count",
            "failed_count",
            "failed_rows_file",
            "file_storage_key",
            "summary_new",
            "summary_exists",
            "summary_conflict",
            "summary_out_of_scope",
        }
    ),
    ResourceKind.POLICY_BINDING: frozenset({"revision_id", "version", "archived_at"}),
    ResourceKind.TENANT: frozenset(
        {
            "tenant_name",
            "status",
            "lifecycle_state",
            "bootstrap_version",
            "bootstrap_key_hash",
            "bootstrap_fingerprint",
        }
    ),
    ResourceKind.PLATFORM_PRINCIPAL: frozenset(
        {"hashed_password", "status", "permissions"}
    ),
    ResourceKind.PROVIDER: frozenset(
        {"provider_code", "name", "api_key", "base_url", "is_enabled", "config"}
    ),
    ResourceKind.MODEL: frozenset(
        {
            "provider_id",
            "name",
            "capabilities",
            "base_url",
            "is_enabled",
            "sort_order",
            "config",
        }
    ),
    ResourceKind.AGENT: frozenset(
        {
            "name",
            "description",
            "i18n_keys",
            "enabled",
            "display_order",
            "system_prompt",
            "model_preference",
            "daily_quota_per_user",
            "risk_appetite",
        }
    ),
    ResourceKind.TENANT_MODEL_POLICY: frozenset(
        {"enabled", "is_default", "daily_quota_per_user"}
    ),
    ResourceKind.ROLE_AGENT: frozenset({"enabled"}),
    ResourceKind.MENU: frozenset(
        {
            "parent_id",
            "menu_name",
            "menu_type",
            "icon",
            "icon_type",
            "path",
            "component",
            "route_name",
            "route_path",
            "order",
            "status",
            "update_by",
            "path_param",
            "page",
            "layout",
            "i18n_key",
            "keep_alive",
            "constant",
            "href",
            "hide_in_menu",
            "active_menu",
            "multi_tab",
            "fixed_index_in_tab",
            "permission",
            "query",
        }
    ),
    ResourceKind.SETTING: frozenset({"setting_value", "status"}),
    ResourceKind.CONFIG: frozenset(
        {
            "config_name",
            "config_key",
            "config_value",
            "config_type",
            "config_group",
            "status",
            "is_public",
            "remark",
            "update_by",
        }
    ),
    ResourceKind.MAIL_CONFIG: frozenset(
        {"values", "password_ciphertext", "version", "last_test_at", "last_test_status"}
    ),
}
_GLOBAL_KINDS = frozenset(
    {
        ResourceKind.PROVIDER,
        ResourceKind.MODEL,
        ResourceKind.AGENT,
        ResourceKind.PLATFORM_PRINCIPAL,
        ResourceKind.MAIL_CONFIG,
    }
)
_AUDIT_KINDS = frozenset(
    {ResourceKind.SYSTEM_OPERATION_LOG, ResourceKind.PLATFORM_AUDIT}
)
_SECURITY_KEYS = frozenset(
    key for key, item in SETTINGS.items() if item.group == "security"
)
_AUTHORIZATION_KINDS = (
    frozenset(
        {
            ResourceKind.USER,
            ResourceKind.ROLE,
            ResourceKind.DEPT,
            ResourceKind.USER_ROLES,
            ResourceKind.USER_DEPTS,
            ResourceKind.ROLE_DEPTS,
            ResourceKind.ROLE_MENUS,
            ResourceKind.ROLE_AGENT,
            ResourceKind.TENANT,
            ResourceKind.MENU,
            ResourceKind.SETTING,
            ResourceKind.CONFIG,
            ResourceKind.TENANT_MODEL_POLICY,
        }
    )
    | _GLOBAL_KINDS
)
_DELETABLE_KINDS = _AUTHORIZATION_KINDS - {
    ResourceKind.TENANT,
    ResourceKind.PLATFORM_PRINCIPAL,
    ResourceKind.MAIL_CONFIG,
}
_POLICY_KINDS = frozenset(
    {
        ResourceKind.POLICY_BINDING,
        ResourceKind.POLICY_REVISION,
        ResourceKind.POLICY_AUDIT,
    }
)
# Fixed historical tables remain relevant even though marketplace is not an
# enabled application module. Never let a user DELETE silently modify them.
_MARKETPLACE_USER_DEPENDENCIES = (
    (table("mk_app", column("author_id")), "author_id"),
    (table("mk_app_rating", column("user_id")), "user_id"),
)
_CASCADE_DEPENDENCIES = {
    ResourceKind.USER: (
        (ResourceKind.USER_ROLES, "user_id"),
        (ResourceKind.USER_DEPTS, "user_id"),
        (ResourceKind.CONVERSATION, "user_id"),
    ),
    ResourceKind.ROLE: (
        (ResourceKind.USER_ROLES, "role_id"),
        (ResourceKind.ROLE_DEPTS, "role_id"),
        (ResourceKind.ROLE_MENUS, "role_id"),
        (ResourceKind.ROLE_AGENT, "role_id"),
    ),
    ResourceKind.DEPT: (
        (ResourceKind.USER_DEPTS, "dept_id"),
        (ResourceKind.ROLE_DEPTS, "dept_id"),
    ),
    ResourceKind.PROVIDER: ((ResourceKind.MODEL, "provider_id"),),
    ResourceKind.MODEL: ((ResourceKind.TENANT_MODEL_POLICY, "model_id"),),
    ResourceKind.AGENT: ((ResourceKind.ROLE_AGENT, "agent_id"),),
    ResourceKind.MENU: (
        (ResourceKind.ROLE_MENUS, "menu_id"),
        (ResourceKind.MENU, "parent_id"),
    ),
}


def _unavailable():
    return BusinessException(
        503, "授权屏障状态不可用", error_code="FIELD_POLICY_UNAVAILABLE"
    )


def _limit_error():
    return BusinessException(
        422, "完整授权计划超过上限", error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
    )


def _stale():
    return BusinessException(
        409, "授权资源快照已经变化", error_code="AUTHORIZATION_SNAPSHOT_STALE"
    )


def _plain_value(value, *, depth=0):
    if depth > 32:
        return False
    if value is None or type(value) in {
        str,
        bool,
        int,
        float,
        bytes,
        date,
        datetime,
        Decimal,
    }:
        return True
    if type(value) in {tuple, list}:
        return all(_plain_value(item, depth=depth + 1) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and _plain_value(item, depth=depth + 1)
            for key, item in value.items()
        )
    return False


class _Transaction:
    def __init__(self, db, budget, global_mode, deadline):
        self._db = db
        self._journal = db._authorization_journal
        self._budget = budget
        self._global_mode = global_mode
        self._deadline = deadline
        self._barrier_deadline = min(
            deadline, asyncio.get_running_loop().time() + budget.lock_timeout_ms / 1000
        )
        self._outer = None
        self._task = asyncio.current_task()
        self._tenants = {}
        self._plan = None
        self._held = frozenset()
        self._owned = set()
        self._attempted = set()
        self._frames = []
        self._all_tenants = False
        self._deleted = set()
        self._mutations = {}
        self._aggregate_tenant = None
        self._aggregate_global = False
        self._global_mutations = 0
        self._creation = None
        self._retained_count = 0
        self._policy = None

    def _check(self, *, locked=False):
        journal = getattr(self, "_journal", None)
        if journal is None:
            raise _order_error()
        if (
            journal.handle is not self
            or journal.phase != "active"
            or journal.outer is not self._outer
            or self._db.sync_session.get_transaction() is not self._outer
            or not self._outer.is_active
            or journal.connection is None
            or journal.connection.closed
            or journal.connection.invalidated
            or asyncio.current_task() is not self._task
            or (locked and self._plan is None)
        ):
            raise _order_error()
        journal.check_time()

    async def _execute(self, statement, parameters=None):
        journal = self._journal
        journal.check_time()
        token = object()
        journal.permit = token
        journal.recoverable_insert = isinstance(statement, (Insert, Update)) and bool(
            self._frames
        )
        try:
            return await self._db.execute(
                statement,
                parameters,
                execution_options={
                    "_authorization_plan_permit": token,
                    "compiled_cache": None,
                },
            )
        except IntegrityError:
            if not self._frames:
                journal.fail()
            raise
        except DBAPIError as exc:
            journal.fail()
            if getattr(exc.orig, "sqlstate", None) in {"55P03", "57014", "40P01"}:
                raise _unavailable() from None
            raise
        except BaseException:
            journal.fail()
            raise
        finally:
            journal.permit = None
            journal.recoverable_insert = False

    async def _timeouts(self, *, barrier=False):
        deadline = self._barrier_deadline if barrier else self._deadline
        remaining = math.ceil((deadline - asyncio.get_running_loop().time()) * 1000)
        if remaining <= 0:
            self._journal.fail()
            raise _timeout_error()
        await self._execute(
            text(
                "SELECT set_config('lock_timeout', :lock, true), "
                "set_config('statement_timeout', :statement, true)"
            ),
            {
                "lock": f"{min(remaining, self._budget.lock_timeout_ms)}ms",
                "statement": f"{min(remaining, self._budget.statement_timeout_ms)}ms",
            },
        )

    async def _global(self):
        await self._timeouts(barrier=True)
        table = UserFieldPolicyGlobalGuard.__table__
        result = await self._execute(
            select(table.c.epoch)
            .where(table.c.guard_id == 1)
            .with_for_update(read=self._global_mode == LockMode.SHARED)
        )
        epoch = result.scalar_one_or_none()
        if epoch is None or epoch < 1:
            raise _unavailable()

    async def _enumerate(self):
        last = -1
        tenants = []
        while True:
            await self._timeouts(barrier=True)
            rows = (
                (
                    await self._execute(
                        select(Tenant.tenant_id)
                        .where(Tenant.tenant_id > last)
                        .order_by(Tenant.tenant_id)
                        .limit(min(256, 1025 - len(tenants)))
                    )
                )
                .scalars()
                .all()
            )
            tenants.extend(TenantLock(i, LockMode.EXCLUSIVE) for i in rows)
            if len(tenants) > 1024:
                raise _limit_error()
            if len(rows) < 256:
                return tuple(tenants)
            last = rows[-1]

    async def _barriers(self, tenants):
        table = UserFieldPolicyState.__table__
        expected_digest = catalog_digest()
        # Contiguous same-mode groups preserve the one total tenant order.
        for mode, group in groupby(tenants, key=lambda item: item.mode):
            ids = [item.tenant_id for item in group]
            for offset in range(0, len(ids), 256):
                batch = ids[offset : offset + 256]
                await self._timeouts(barrier=True)
                rows = (
                    (
                        await self._execute(
                            select(table)
                            .where(table.c.tenant_id.in_(batch))
                            .order_by(table.c.tenant_id)
                            .with_for_update(read=mode == LockMode.SHARED)
                        )
                    )
                    .mappings()
                    .all()
                )
                if [row["tenant_id"] for row in rows] != batch:
                    raise _unavailable()
                for row in rows:
                    if (
                        row["object_type"] != "user"
                        or row["mode"] not in {"legacy", "ready"}
                        or row["ever_enforced"]
                        or row["active_manifest_digest"] is not None
                        or row["epoch"] < 1
                        or row["catalog_revision"] != CATALOG_REVISION
                        or row["catalog_digest"] != expected_digest
                        or (
                            row["mode"] == "ready"
                            and row["candidate_manifest_digest"] is None
                        )
                    ):
                        raise _unavailable()
                self._tenants.update((i, mode) for i in batch)

    def _scope_key(self, key, *, new=False):
        if not isinstance(key, ResourceKey):
            raise _order_error()
        structural_source = (
            self._creation is not None
            and self._creation.get("created") is not None
            and key.kind is ResourceKind.SYSTEM_OPERATION_LOG
            and key.tenant_id == 0
        )
        if key.kind is ResourceKind.TENANT:
            target = key_values(key)[0]
            if target not in self._tenants and not (
                self._creation is not None
                and self._creation.get("created") is not None
                and target == 0
            ):
                raise _order_error()
        elif key.tenant_id is None:
            if (
                new
                and key.kind not in _AUDIT_KINDS
                and (self._global_mode != LockMode.EXCLUSIVE or not self._all_tenants)
            ):
                raise _order_error()
        elif key.tenant_id not in self._tenants and not structural_source:
            raise _order_error()
        if new and (
            self._global_mode != LockMode.EXCLUSIVE
            and self._tenants.get(key.tenant_id) != LockMode.EXCLUSIVE
            and key.kind not in _AUDIT_KINDS
        ):
            raise _order_error()
        if new and key.kind is ResourceKind.TENANT:
            # Tenant creation needs the separate structural G-X protocol that
            # creates and then freezes the newly-triggered B row.
            raise _order_error()

    async def lock(self, plan: LockPlan) -> None:
        try:
            self._check()
            if self._plan is not None or not isinstance(plan, LockPlan) or self._frames:
                raise _order_error()
            if self._creation is not None and not self._creation["selected"]:
                raise _order_error()
            for key in plan.existing:
                self._scope_key(key)
            for reserved in plan.new:
                self._scope_key(reserved.key, new=True)
            for (kind, tenant_id), group in groupby(
                plan.existing, key=lambda key: (key.kind, key.tenant_id)
            ):
                spec = resource_spec(kind)
                table = spec.table
                columns = tuple(table.c[name] for name in spec.key_columns)
                keys = list(group)
                for offset in range(0, len(keys), 256):
                    batch = keys[offset : offset + 256]
                    values = [key_values(key) for key in batch]
                    statement = select(*columns).where(tuple_(*columns).in_(values))
                    if spec.tenant_column:
                        statement = statement.where(
                            table.c[spec.tenant_column] == tenant_id
                        )
                    ordered = tuple(
                        column.collate("C")
                        if isinstance(values[0][index], str)
                        else column
                        for index, column in enumerate(columns)
                    )
                    statement = statement.order_by(*ordered).with_for_update()
                    await self._timeouts()
                    rows = (await self._execute(statement)).all()
                    if [tuple(row) for row in rows] != values:
                        raise _stale()
            self._plan = plan
            self._held = frozenset(plan.existing)
        except BaseException:
            self._journal.fail()
            raise

    def assert_held(self, *keys: ResourceKey) -> None:
        self._check(locked=True)
        if any(type(key) is not ResourceKey for key in keys) or any(
            key in self._deleted or (key not in self._held and key not in self._owned)
            for key in keys
        ):
            raise _order_error()

    def assert_session(self, db) -> None:
        self._check()
        if db is not self._db:
            raise _order_error()

    def _require_exclusive_tenant(self, tenant_id):
        if (
            type(tenant_id) is not int
            or self._tenants.get(tenant_id) != LockMode.EXCLUSIVE
        ):
            raise _order_error()

    def _fact_tenant(self, key):
        return key_values(key)[0] if key.kind is ResourceKind.TENANT else key.tenant_id

    def _global_fact(self, key):
        return key.kind in _GLOBAL_KINDS or (
            key.kind is ResourceKind.SETTING and key_values(key)[0] in _SECURITY_KEYS
        )

    def _fact_context(self, key):
        if self._global_fact(key):
            if not self._aggregate_global or not self._all_tenants:
                raise _order_error()
            if key.kind is ResourceKind.SETTING and key.tenant_id != 0:
                raise _order_error()
        else:
            tenant_id = self._fact_tenant(key)
            self._require_exclusive_tenant(tenant_id)
            if not self._aggregate_global and tenant_id != self._aggregate_tenant:
                raise _order_error()

    def _require_mutation_parents(self, key, values):
        if key.kind is ResourceKind.ROLE_AGENT:
            role_id, agent_id = key_values(key)
            self.assert_held(
                ResourceKey(ResourceKind.ROLE, key.tenant_id, role_id),
                ResourceKey(ResourceKind.AGENT, None, agent_id),
            )
        elif key.kind is ResourceKind.TENANT_MODEL_POLICY:
            self.assert_held(ResourceKey(ResourceKind.MODEL, None, key_values(key)[0]))
        elif key.kind is ResourceKind.MODEL and "provider_id" in values:
            self.assert_held(
                ResourceKey(ResourceKind.PROVIDER, None, values["provider_id"])
            )
        elif key.kind is ResourceKind.MENU and values.get("parent_id") is not None:
            self.assert_held(
                ResourceKey(ResourceKind.MENU, key.tenant_id, values["parent_id"])
            )

    def _mutation_context(self, key):
        self.assert_held(key)
        if self._policy is not None:
            self._require_exclusive_tenant(key.tenant_id)
            if (
                key.tenant_id != self._policy["tenant_id"]
                or key.kind is not ResourceKind.POLICY_BINDING
                or self._policy["completed"]
                or self._policy["action"] == "ready"
            ):
                raise _order_error()
        else:
            if key.kind in _POLICY_KINDS:
                raise _order_error()
            self._fact_context(key)

    def _record_mutation(self, key):
        if key.kind in _AUTHORIZATION_KINDS:
            if self._global_fact(key):
                self._global_mutations += 1
            else:
                tenant_id = self._fact_tenant(key)
                self._mutations[tenant_id] = self._mutations.get(tenant_id, 0) + 1
        if (
            self._policy is not None
            and key.kind is ResourceKind.POLICY_BINDING
            and key.tenant_id == self._policy["tenant_id"]
        ):
            self._policy["binding_changes"] += 1

    def _key_predicates(self, key):
        spec = resource_spec(key.kind)
        predicates = [
            spec.table.c[name] == value
            for name, value in zip(spec.key_columns, key_values(key), strict=True)
        ]
        if spec.tenant_column:
            predicates.append(spec.table.c[spec.tenant_column] == key.tenant_id)
        return spec, predicates

    async def _mutation_snapshot(self, key, changes, expected):
        spec, predicates = self._key_predicates(key)
        if type(changes) is not dict or (
            expected is not None and type(expected) is not dict
        ):
            raise _order_error()
        expected = expected or {}
        if any(
            type(name) is not str or name not in spec.table.c or not _plain_value(value)
            for values in (changes, expected)
            for name, value in values.items()
        ):
            raise _order_error()
        names = sorted(set(changes) | set(expected) | set(spec.key_columns))
        await self._timeouts()
        current = (
            (
                await self._execute(
                    select(*(spec.table.c[name] for name in names)).where(*predicates)
                )
            )
            .mappings()
            .one_or_none()
        )
        if current is None or any(
            current[name] != value
            or (type(current[name]) is bool) != (type(value) is bool)
            for name, value in expected.items()
        ):
            raise _stale()
        return spec, predicates, current

    async def update_held(self, key: ResourceKey, changes: dict, expected=None) -> bool:
        try:
            self._mutation_context(key)
            if type(changes) is not dict or key.kind not in _MUTABLE_COLUMNS:
                raise _order_error()
            if any(name not in _MUTABLE_COLUMNS[key.kind] for name in changes):
                raise _order_error()
            self._require_mutation_parents(key, changes)
            spec, predicates, current = await self._mutation_snapshot(
                key, changes, expected
            )
            if all(current[name] == value for name, value in changes.items()):
                return False
            await self._timeouts()
            row = (
                await self._execute(
                    update(spec.table)
                    .where(*predicates)
                    .values(**changes)
                    .returning(*(spec.table.c[name] for name in spec.key_columns))
                )
            ).one_or_none()
            if row is None or tuple(row) != key_values(key):
                raise _stale()
            self._record_mutation(key)
            return True
        except IntegrityError:
            if not self._frames:
                self._journal.fail()
            raise
        except BaseException:
            self._journal.fail()
            raise

    async def _reject_implicit_deletes(self, key):
        for kind, foreign_key in _CASCADE_DEPENDENCIES.get(key.kind, ()):
            child = resource_spec(kind).table
            predicates = [child.c[foreign_key] == key.identity[0]]
            if key.tenant_id is not None:
                predicates.append(child.c.tenant_id == key.tenant_id)
            await self._timeouts()
            found = (
                await self._execute(
                    select(child.c[foreign_key]).where(*predicates).limit(1)
                )
            ).first()
            if found is not None:
                raise _order_error()
        if key.kind is ResourceKind.USER:
            for child, foreign_key in _MARKETPLACE_USER_DEPENDENCIES:
                await self._timeouts()
                found = (
                    await self._execute(
                        select(child.c[foreign_key])
                        .where(child.c[foreign_key] == key.identity[0])
                        .limit(1)
                    )
                ).first()
                if found is not None:
                    raise _order_error()

    async def delete_held(self, key: ResourceKey, expected=None) -> bool:
        try:
            self._mutation_context(key)
            if key.kind not in _DELETABLE_KINDS:
                raise _order_error()
            spec, predicates, _ = await self._mutation_snapshot(key, {}, expected)
            await self._reject_implicit_deletes(key)
            await self._timeouts()
            row = (
                await self._execute(
                    delete(spec.table)
                    .where(*predicates)
                    .returning(*(spec.table.c[name] for name in spec.key_columns))
                )
            ).one_or_none()
            if row is None or tuple(row) != key_values(key):
                raise _stale()
            self._deleted.add(key)
            self._record_mutation(key)
            return True
        except BaseException:
            self._journal.fail()
            raise

    @asynccontextmanager
    async def aggregate(self, tenant_id):
        try:
            self._check(locked=True)
            self._require_exclusive_tenant(tenant_id)
            if (
                self._aggregate_tenant is not None
                or self._policy is not None
                or self._aggregate_global
            ):
                raise _order_error()
            self._aggregate_tenant = tenant_id
            before = self._mutations.get(tenant_id, 0)
            yield
            self._check(locked=True)
            if self._mutations.get(tenant_id, 0) != before and not (
                self._creation is not None
                and self._creation.get("created") == tenant_id
            ):
                await self._bump_tenants((tenant_id,))
        except BaseException:
            self._journal.fail()
            raise
        finally:
            self._aggregate_tenant = None

    async def _bump_tenants(self, tenant_ids):
        state = UserFieldPolicyState.__table__
        tenant_ids = sorted(tenant_ids)
        for offset in range(0, len(tenant_ids), 256):
            batch = tenant_ids[offset : offset + 256]
            await self._timeouts()
            rows = (
                (
                    await self._execute(
                        update(state)
                        .where(state.c.tenant_id.in_(batch))
                        .values(epoch=state.c.epoch + 1, mode="legacy")
                        .returning(state.c.tenant_id)
                    )
                )
                .scalars()
                .all()
            )
            if sorted(rows) != batch:
                raise _unavailable()

    async def _bump_global(self):
        guard = UserFieldPolicyGlobalGuard.__table__
        await self._timeouts()
        value = (
            await self._execute(
                update(guard)
                .where(guard.c.guard_id == 1)
                .values(epoch=guard.c.epoch + 1)
                .returning(guard.c.epoch)
            )
        ).scalar_one_or_none()
        if value is None:
            raise _unavailable()

    @asynccontextmanager
    async def aggregate_global(self):
        try:
            self._check(locked=True)
            if (
                not self._all_tenants
                or self._global_mode != LockMode.EXCLUSIVE
                or self._aggregate_global
                or self._aggregate_tenant is not None
                or self._policy is not None
            ):
                raise _order_error()
            self._aggregate_global = True
            before_global = self._global_mutations
            before = dict(self._mutations)
            yield
            self._check(locked=True)
            if self._global_mutations != before_global:
                await self._bump_global()
                await self._bump_tenants(self._tenants)
            else:
                await self._bump_tenants(
                    tenant_id
                    for tenant_id, count in self._mutations.items()
                    if count != before.get(tenant_id, 0)
                )
        except BaseException:
            self._journal.fail()
            raise
        finally:
            self._aggregate_global = False

    @asynccontextmanager
    async def policy_change(self, tenant_id, *, action):
        try:
            self._check(locked=True)
            self._require_exclusive_tenant(tenant_id)
            if (
                self._policy is not None
                or self._aggregate_tenant is not None
                or self._aggregate_global
                or type(action) is not str
                or action not in {"publish", "archive", "ready"}
            ):
                raise _order_error()
            self._policy = {
                "tenant_id": tenant_id,
                "action": action,
                "binding_changes": 0,
                "completed": False,
                "audit_count": 0,
                "epoch": None,
                "manifest": None,
            }
            yield
            self._check(locked=True)
            if not self._policy["completed"] or self._policy["audit_count"] != 1:
                raise _order_error()
        except BaseException:
            self._journal.fail()
            raise
        finally:
            self._policy = None

    async def complete_policy_change(
        self, *, expected_epoch, expected_manifest, manifest_digest
    ):
        try:
            self._check(locked=True)
            frame = self._policy
            if (
                frame is None
                or frame["completed"]
                or type(expected_epoch) is not int
                or not 1 <= expected_epoch < 2**63 - 1
                or type(manifest_digest) is not str
                or re.fullmatch(r"[0-9a-f]{64}", manifest_digest) is None
                or (
                    expected_manifest is not None
                    and (
                        type(expected_manifest) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", expected_manifest) is None
                    )
                )
                or (
                    frame["action"] == "ready"
                    and (
                        frame["binding_changes"] or expected_manifest != manifest_digest
                    )
                )
                or (frame["action"] != "ready" and not frame["binding_changes"])
            ):
                raise _order_error()
            state = UserFieldPolicyState.__table__
            await self._timeouts()
            epoch = (
                await self._execute(
                    update(state)
                    .where(
                        state.c.tenant_id == frame["tenant_id"],
                        state.c.epoch == expected_epoch,
                        state.c.candidate_manifest_digest.is_not_distinct_from(
                            expected_manifest
                        ),
                    )
                    .values(
                        epoch=state.c.epoch + 1,
                        mode="ready" if frame["action"] == "ready" else "legacy",
                        candidate_manifest_digest=manifest_digest,
                    )
                    .returning(state.c.epoch)
                )
            ).scalar_one_or_none()
            if epoch is None:
                raise _stale()
            frame.update(completed=True, epoch=epoch, manifest=manifest_digest)
            return epoch
        except BaseException:
            self._journal.fail()
            raise

    def _creation_context(self):
        self._check()
        if (
            self._creation is None
            or self._creation["selected"]
            or self._plan is not None
            or self._tenants
            or self._frames
            or self._global_mode != LockMode.EXCLUSIVE
        ):
            raise _order_error()

    async def create_tenant(self, key: ResourceKey, values: dict) -> None:
        try:
            self._creation_context()
            if (
                type(key) is not ResourceKey
                or key.kind is not ResourceKind.TENANT
                or key_values(key)[0] == 0
                or type(values) is not dict
            ):
                raise _order_error()
            fixed = {
                "tenant_id": key_values(key)[0],
                "status": "2",
                "lifecycle_state": "prepared",
                "row_version": 1,
                "bootstrap_version": 0,
            }
            allowed = {
                "tenant_code",
                "tenant_name",
                "provisioning_key_hash",
                "provisioning_fingerprint",
            } | fixed.keys()
            if (
                set(values) - allowed
                or not {"tenant_code", "tenant_name"} <= values.keys()
                or any(
                    name in values
                    and (values[name] != value or type(values[name]) is not type(value))
                    for name, value in fixed.items()
                )
                or any(not _plain_value(value) for value in values.values())
            ):
                raise _order_error()
            async with asyncio.timeout_at(self._barrier_deadline):
                tenants = await self._enumerate()
                if len(tenants) >= 1024:
                    raise _limit_error()
                await self._timeouts(barrier=True)
                identity = (
                    await self._execute(
                        insert(Tenant.__table__)
                        .values(**(values | fixed))
                        .returning(Tenant.tenant_id)
                    )
                ).scalar_one()
                if identity != key_values(key)[0]:
                    raise _stale()
                await self._barriers((TenantLock(identity, LockMode.EXCLUSIVE),))
                state = UserFieldPolicyState.__table__
                row = (
                    await self._execute(
                        select(
                            state.c.epoch,
                            state.c.mode,
                            state.c.candidate_manifest_digest,
                        ).where(state.c.tenant_id == identity)
                    )
                ).one()
                if tuple(row) != (1, "legacy", None):
                    raise _unavailable()
            self._owned.add(key)
            self._creation.update(selected=True, created=identity)
            await self._timeouts()
        except TimeoutError:
            self._journal.fail()
            raise _timeout_error() from None
        except BaseException:
            self._journal.fail()
            raise

    async def replay_tenants(self, tenants: tuple[TenantLock, ...]) -> None:
        try:
            self._creation_context()
            if (
                type(tenants) is not tuple
                or not tenants
                or len(tenants) > 32
                or any(type(item) is not TenantLock for item in tenants)
                or len({item.tenant_id for item in tenants}) != len(tenants)
            ):
                raise _order_error()
            async with asyncio.timeout_at(self._barrier_deadline):
                await self._barriers(
                    tuple(sorted(tenants, key=lambda item: item.tenant_id))
                )
            self._creation["selected"] = True
            await self._timeouts()
        except TimeoutError:
            self._journal.fail()
            raise _timeout_error() from None
        except BaseException:
            self._journal.fail()
            raise

    async def update_metadata(
        self, key: ResourceKey, changes: dict, expected=None
    ) -> bool:
        try:
            self.assert_held(key)
            if (
                self._policy is not None
                or self._aggregate_tenant is not None
                or self._aggregate_global
            ):
                raise _order_error()
            allowed = {
                ResourceKind.PLATFORM_PRINCIPAL: {"last_login_at"},
                ResourceKind.MAIL_CONFIG: {
                    "last_test_at",
                    "last_test_status",
                    "version",
                },
            }.get(key.kind)
            if allowed is None or type(changes) is not dict or set(changes) - allowed:
                raise _order_error()
            if type(expected) is not dict:
                raise _order_error()
            version_name = (
                "version" if key.kind is ResourceKind.MAIL_CONFIG else "row_version"
            )
            if (
                type(expected.get(version_name)) is not int
                or expected[version_name] < 1
            ):
                raise _order_error()
            if "version" in changes and (
                type(changes["version"]) is not int
                or changes["version"] != expected["version"] + 1
            ):
                raise _order_error()
            spec, predicates, current = await self._mutation_snapshot(
                key, changes, expected
            )
            if all(current[name] == value for name, value in changes.items()):
                return False
            await self._timeouts()
            row = (
                await self._execute(
                    update(spec.table)
                    .where(*predicates)
                    .values(**changes)
                    .returning(*(spec.table.c[name] for name in spec.key_columns))
                )
            ).one_or_none()
            if row is None or tuple(row) != key_values(key):
                raise _stale()
            return True
        except BaseException:
            self._journal.fail()
            raise

    async def delete_retained(self, key: ResourceKey, expected=None) -> bool:
        try:
            self.assert_held(key)
            self._require_exclusive_tenant(key.tenant_id)
            if (
                key.kind
                not in {ResourceKind.SYSTEM_OPERATION_LOG, ResourceKind.LOGIN_LOG}
                or self._policy is not None
                or self._aggregate_tenant is not None
                or self._aggregate_global
                or self._retained_count >= 10000
                or (expected is not None and type(expected) is not dict)
            ):
                raise _order_error()
            expected = expected or {}
            if expected.get("audit_scope", "tenant") != "tenant":
                raise _order_error()
            spec, predicates, _ = await self._mutation_snapshot(
                key, {}, expected | {"audit_scope": "tenant"}
            )
            await self._timeouts()
            row = (
                await self._execute(
                    delete(spec.table)
                    .where(*predicates)
                    .returning(*(spec.table.c[name] for name in spec.key_columns))
                )
            ).one_or_none()
            if row is None or tuple(row) != key_values(key):
                raise _stale()
            self._deleted.add(key)
            self._retained_count += 1
            return True
        except BaseException:
            self._journal.fail()
            raise

    async def append_audit(self, key: ResourceKey, values: dict) -> int:
        try:
            self._check(locked=True)
            if (
                type(key) is not ResourceKey
                or key.kind not in _AUDIT_KINDS
                or key not in {item.key for item in self._plan.new}
                or key in self._attempted
                or type(values) is not dict
                or self._policy is not None
            ):
                raise _order_error()
            spec = resource_spec(key.kind)
            fixed = dict(zip(spec.key_columns, key_values(key), strict=True))
            if spec.tenant_column:
                fixed[spec.tenant_column] = key.tenant_id
            time_column = (
                "create_time"
                if key.kind is ResourceKind.SYSTEM_OPERATION_LOG
                else "created_at"
            )
            if (
                any(name not in spec.table.c or name == time_column for name in values)
                or any(not _plain_value(value) for value in values.values())
                or any(
                    name in values
                    and (values[name] != value or type(values[name]) is not type(value))
                    for name, value in fixed.items()
                )
            ):
                raise _order_error()
            if key.kind is ResourceKind.SYSTEM_OPERATION_LOG:
                self.assert_held(ResourceKey(ResourceKind.TENANT, None, key.tenant_id))
                if (
                    self._creation is not None
                    and self._creation.get("created") is not None
                    and key.tenant_id == 0
                    and values.get("audit_scope") != "platform"
                ):
                    raise _order_error()
            else:
                principal = values.get("actor_principal_id")
                self.assert_held(
                    ResourceKey(ResourceKind.PLATFORM_PRINCIPAL, None, principal)
                )
                if values.get("event_type") == "completed":
                    authorization = values.get("authorization_audit_id")
                    self.assert_held(
                        ResourceKey(ResourceKind.PLATFORM_AUDIT, None, authorization)
                    )
                    if type(values.get("result_summary")) is not dict or values[
                        "result_summary"
                    ].get("statusCode") != values.get("status_code"):
                        raise _order_error()
            self._attempted.add(key)
            payload = values | fixed | {time_column: func.clock_timestamp()}
            statement = pg_insert(spec.table).values(**payload)
            completion = (
                key.kind is ResourceKind.PLATFORM_AUDIT
                and values.get("event_type") == "completed"
            )
            if completion:
                statement = statement.on_conflict_do_nothing(
                    index_elements=[spec.table.c.authorization_audit_id],
                    index_where=spec.table.c.authorization_audit_id.is_not(None),
                )
            await self._timeouts()
            actual = (
                await self._execute(
                    statement.returning(spec.table.c[spec.key_columns[0]])
                )
            ).scalar_one_or_none()
            if actual is not None:
                if actual != key_values(key)[0]:
                    raise _stale()
                self._owned.add(key)
                return actual
            if not completion:
                raise _stale()
            await self._timeouts()
            prior = (
                await self._execute(
                    select(spec.table.c.audit_id, spec.table.c.status_code).where(
                        spec.table.c.authorization_audit_id
                        == values["authorization_audit_id"]
                    )
                )
            ).one_or_none()
            if prior is None:
                raise _unavailable()
            if prior.status_code != values["status_code"]:
                raise BusinessException(
                    409,
                    "平台完成审计状态冲突",
                    error_code="PLATFORM_AUDIT_COMPLETION_CONFLICT",
                )
            return prior.audit_id
        except BaseException:
            self._journal.fail()
            raise

    async def insert_reserved(self, key: ResourceKey, values: dict) -> None:
        self._check(locked=True)
        try:
            if (
                type(key) is not ResourceKey
                or key.kind in _AUDIT_KINDS
                or key.kind is ResourceKind.TENANT
            ):
                raise _order_error()
            if key.kind in _AUTHORIZATION_KINDS:
                self._fact_context(key)
            if key.kind in _POLICY_KINDS and self._policy is None:
                raise _order_error()
            if self._aggregate_tenant is not None and (
                key.tenant_id != self._aggregate_tenant or key.kind in _POLICY_KINDS
            ):
                raise _order_error()
            if self._policy is not None:
                frame = self._policy
                if key.tenant_id != frame["tenant_id"] or key.kind not in _POLICY_KINDS:
                    raise _order_error()
                if key.kind is ResourceKind.POLICY_AUDIT:
                    if (
                        not frame["completed"]
                        or frame["audit_count"]
                        or type(values) is not dict
                        or values.get("action") != frame["action"]
                        or values.get("epoch") != frame["epoch"]
                        or values.get("after_digest") != frame["manifest"]
                    ):
                        raise _order_error()
                elif frame["completed"] or frame["action"] == "ready":
                    raise _order_error()
            if (
                key not in {item.key for item in self._plan.new}
                or key in self._attempted
                or not isinstance(values, dict)
            ):
                raise _order_error()
            spec = resource_spec(key.kind)
            fixed = dict(zip(spec.key_columns, key_values(key), strict=True))
            if spec.tenant_column:
                fixed[spec.tenant_column] = key.tenant_id
            if any(
                name in values and values[name] != value
                for name, value in fixed.items()
            ):
                raise _order_error()
            if any(name not in spec.table.c for name in values):
                raise _order_error()
            self._require_mutation_parents(key, values)
            if any(not _plain_value(value) for value in values.values()) or any(
                column.name in values and column.name not in fixed
                for column in spec.table.primary_key
            ):
                raise _order_error()
            self._attempted.add(key)
            await self._timeouts()
            row = (
                await self._execute(
                    insert(spec.table)
                    .values(**values | fixed)
                    .returning(*(spec.table.c[name] for name in spec.key_columns))
                )
            ).one()
            if tuple(row) != key_values(key):
                raise _stale()
            self._owned.add(key)
            self._record_mutation(key)
            if self._policy is not None and key.kind is ResourceKind.POLICY_AUDIT:
                self._policy["audit_count"] += 1
        except IntegrityError:
            if not self._frames:
                self._journal.fail()
            raise
        except BaseException:
            self._journal.fail()
            raise

    @asynccontextmanager
    async def savepoint(self):
        self._check(locked=True)
        if self._db.new or self._db.dirty or self._db.deleted:
            self._journal.reject()
        checkpoint = set(self._owned)
        deleted_checkpoint = set(self._deleted)
        mutation_checkpoint = dict(self._mutations)
        global_checkpoint = self._global_mutations
        retained_checkpoint = self._retained_count
        policy_checkpoint = dict(self._policy) if self._policy is not None else None
        self._journal.control += 1
        try:
            nested = await AsyncSession.begin_nested(self._db)
            await self._db.connection()  # Emit SAVEPOINT inside the control permit.
        except BaseException:
            self._journal.fail()
            raise
        finally:
            self._journal.control -= 1
        self._frames.append(nested)
        try:
            yield
            self._check(locked=True)
            self._journal.control += 1
            try:
                await nested.commit()
            except BaseException:
                self._journal.fail()
                raise
            finally:
                self._journal.control -= 1
        except BaseException as exc:
            self._owned.intersection_update(checkpoint)
            self._deleted = deleted_checkpoint
            self._mutations = mutation_checkpoint
            self._global_mutations = global_checkpoint
            self._retained_count = retained_checkpoint
            if self._policy is not None and policy_checkpoint is not None:
                self._policy.clear()
                self._policy.update(policy_checkpoint)
            if isinstance(exc, (asyncio.CancelledError, TimeoutError)):
                self._journal.fail()
            if self._journal.phase == "active":
                self._journal.control += 1
                try:
                    await nested.rollback()
                except BaseException:
                    self._journal.fail()
                    raise
                finally:
                    self._journal.control -= 1
            raise
        finally:
            self._frames.pop()


class AuthorizationTransactionService:
    @asynccontextmanager
    async def scope(
        self,
        db,
        *,
        tenants=(),
        global_mode=LockMode.SHARED,
        budget=TransactionBudget(),
    ):
        async with self._scope(db, tenants, global_mode, budget, False) as tx:
            yield tx

    @asynccontextmanager
    async def scope_global(self, db, *, budget=TransactionBudget()):
        async with self._scope(db, (), LockMode.EXCLUSIVE, budget, True) as tx:
            yield tx

    @asynccontextmanager
    async def scope_creation(self, db, *, budget=TransactionBudget(timeout_ms=60000)):
        async with self._scope(
            db, (), LockMode.EXCLUSIVE, budget, False, creation=True
        ) as tx:
            yield tx

    @asynccontextmanager
    async def _scope(
        self, db, tenants, global_mode, budget, enumerate_all, *, creation=False
    ):
        if not isinstance(db, AuthorizationPlanSession):
            raise _order_error()
        journal = db._authorization_journal
        if (
            not journal.valid_bind
            or journal.phase != "idle"
            or journal.unsafe
            or db.in_nested_transaction()
            or db.new
            or db.dirty
            or db.deleted
            or not isinstance(budget, TransactionBudget)
            or not isinstance(global_mode, LockMode)
            or not isinstance(tenants, tuple)
            or any(not isinstance(item, TenantLock) for item in tenants)
        ):
            journal.phase = "failed"
            raise _order_error()
        if len(tenants) > 32:
            journal.phase = "failed"
            raise _limit_error()
        if len({item.tenant_id for item in tenants}) != len(tenants):
            journal.phase = "failed"
            raise _order_error()
        tenants = tuple(sorted(tenants, key=lambda item: item.tenant_id))
        deadline = asyncio.get_running_loop().time() + budget.timeout_ms / 1000
        # Resolve the fixed catalog before holding any database locks. Lazy
        # Python imports must not introduce filesystem IO inside G/B.
        for kind in ResourceKind:
            resource_spec(kind)
        tx = _Transaction(db, budget, global_mode, deadline)
        if creation:
            tx._creation = {"selected": False, "created": None}
        journal.handle = tx
        journal.phase = "preparing"
        journal.deadline = deadline
        try:
            async with asyncio.timeout_at(deadline):
                async with asyncio.timeout_at(tx._barrier_deadline):
                    await db.connection()
                    tx._outer = journal.outer
                    await tx._global()
                    if enumerate_all:
                        tenants = await tx._enumerate()
                        tx._all_tenants = True
                    await tx._barriers(tenants)
                    if asyncio.get_running_loop().time() >= tx._barrier_deadline:
                        raise _timeout_error()
                await tx._timeouts()
                journal.phase = "active"
                yield tx
                tx._check()
                if (
                    tx._frames
                    or tx._aggregate_tenant is not None
                    or tx._aggregate_global
                    or tx._policy is not None
                    or (creation and (not tx._creation["selected"] or tx._plan is None))
                    or db.new
                    or db.dirty
                    or db.deleted
                ):
                    raise _order_error()
                if creation and tx._creation["created"] is not None:
                    await tx._bump_global()
                journal.phase = "closed"
        except TimeoutError:
            journal.fail()
            raise _timeout_error() from None
        except BaseException:
            journal.fail()
            raise


authorization_transactions = AuthorizationTransactionService()
