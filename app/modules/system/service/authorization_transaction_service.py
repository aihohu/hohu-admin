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

from sqlalchemy import column, delete, insert, select, table, text, tuple_, update
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
}
_AUTHORIZATION_KINDS = frozenset(
    {
        ResourceKind.USER,
        ResourceKind.ROLE,
        ResourceKind.DEPT,
        ResourceKind.USER_ROLES,
        ResourceKind.USER_DEPTS,
        ResourceKind.ROLE_DEPTS,
        ResourceKind.ROLE_MENUS,
        ResourceKind.ROLE_AGENT,
    }
)
_DELETABLE_KINDS = _AUTHORIZATION_KINDS
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
        if key.tenant_id is None:
            if self._global_mode != LockMode.EXCLUSIVE or not self._all_tenants:
                raise _order_error()
        elif key.tenant_id not in self._tenants:
            raise _order_error()
        if new and (
            self._global_mode != LockMode.EXCLUSIVE
            and self._tenants.get(key.tenant_id) != LockMode.EXCLUSIVE
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

    def _mutation_context(self, key):
        self.assert_held(key)
        self._require_exclusive_tenant(key.tenant_id)
        if self._policy is not None:
            if (
                key.tenant_id != self._policy["tenant_id"]
                or key.kind is not ResourceKind.POLICY_BINDING
                or self._policy["completed"]
                or self._policy["action"] == "ready"
            ):
                raise _order_error()
        elif key.tenant_id != self._aggregate_tenant or key.kind in _POLICY_KINDS:
            raise _order_error()

    def _record_mutation(self, key):
        if key.kind in _AUTHORIZATION_KINDS and self._aggregate_tenant == key.tenant_id:
            self._mutations[key.tenant_id] = self._mutations.get(key.tenant_id, 0) + 1
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
            await self._timeouts()
            found = (
                await self._execute(
                    select(child.c[foreign_key])
                    .where(
                        child.c.tenant_id == key.tenant_id,
                        child.c[foreign_key] == key.identity[0],
                    )
                    .limit(1)
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
            if self._aggregate_tenant is not None or self._policy is not None:
                raise _order_error()
            self._aggregate_tenant = tenant_id
            before = self._mutations.get(tenant_id, 0)
            yield
            self._check(locked=True)
            if self._mutations.get(tenant_id, 0) != before:
                state = UserFieldPolicyState.__table__
                await self._timeouts()
                epoch = (
                    await self._execute(
                        update(state)
                        .where(state.c.tenant_id == tenant_id)
                        .values(epoch=state.c.epoch + 1, mode="legacy")
                        .returning(state.c.epoch)
                    )
                ).scalar_one_or_none()
                if epoch is None:
                    raise _unavailable()
        except BaseException:
            self._journal.fail()
            raise
        finally:
            self._aggregate_tenant = None

    @asynccontextmanager
    async def policy_change(self, tenant_id, *, action):
        try:
            self._check(locked=True)
            self._require_exclusive_tenant(tenant_id)
            if (
                self._policy is not None
                or self._aggregate_tenant is not None
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

    async def insert_reserved(self, key: ResourceKey, values: dict) -> None:
        self._check(locked=True)
        try:
            if type(key) is not ResourceKey or key.kind is ResourceKind.ROLE_AGENT:
                raise _order_error()
            if (
                key.kind in _AUTHORIZATION_KINDS
                and self._aggregate_tenant != key.tenant_id
            ):
                raise _order_error()
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
    async def _scope(self, db, tenants, global_mode, budget, enumerate_all):
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
                    or tx._policy is not None
                    or db.new
                    or db.dirty
                    or db.deleted
                ):
                    raise _order_error()
                journal.phase = "closed"
        except TimeoutError:
            journal.fail()
            raise _timeout_error() from None
        except BaseException:
            journal.fail()
            raise


authorization_transactions = AuthorizationTransactionService()
