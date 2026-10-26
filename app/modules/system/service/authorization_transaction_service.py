"""Complete authorization lock plans, with database-issued transaction proofs.

The owner supplies authorization and commits after successful scope exit. No
existing writer is implicitly migrated by importing this service.
"""

import asyncio
import math
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from itertools import groupby

from sqlalchemy import insert, select, text, tuple_
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Insert

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
        journal.recoverable_insert = isinstance(statement, Insert) and bool(
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
            key not in self._held and key not in self._owned for key in keys
        ):
            raise _order_error()

    async def insert_reserved(self, key: ResourceKey, values: dict) -> None:
        self._check(locked=True)
        try:
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
                if tx._frames or db.new or db.dirty or db.deleted:
                    raise _order_error()
                journal.phase = "closed"
        except TimeoutError:
            journal.fail()
            raise _timeout_error() from None
        except BaseException:
            journal.fail()
            raise


authorization_transactions = AuthorizationTransactionService()
