"""Opt-in, instance-local SQL history for authorization transaction plans.

Ordinary application sessions are deliberately unaffected. This is an execution
protocol for trusted Python callers, not a sandbox against arbitrary Python code.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql import Select, visitors
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.sql.elements import (
    ColumnClause,
    ReleaseSavepointClause,
    RollbackToSavepointClause,
    SavepointClause,
    TextClause,
)
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.selectable import CompoundSelect

from app.core.exceptions import BusinessException


def _order_error() -> BusinessException:
    return BusinessException(
        409, "授权事务锁顺序或证明无效", error_code="FIELD_POLICY_LOCK_ORDER"
    )


def _timeout_error() -> BusinessException:
    return BusinessException(
        503, "授权事务累计预算已耗尽", error_code="AUTHORIZATION_TRANSACTION_TIMEOUT"
    )


def _known_read(statement: Any) -> bool:
    if not isinstance(statement, (Select, CompoundSelect)):
        return False
    for node in visitors.iterate(statement):
        if isinstance(node, (TextClause, UpdateBase, FunctionElement)):
            return False
        if isinstance(node, ColumnClause) and node.is_literal:
            return False
        if isinstance(node, Select) and node._for_update_arg is not None:
            return False
    return True


@dataclass
class _Journal:
    session: Any
    valid_bind: bool
    outer: Any = None
    connection: Any = None
    unsafe: bool = False
    phase: str = "idle"
    handle: Any = None
    permit: Any = None
    control: int = 0
    committing: bool = False
    deadline: float = 0.0
    recoverable_insert: bool = False
    cleaning: bool = False

    def io_failed(self, error):
        if isinstance(error, IntegrityError) and self.recoverable_insert:
            return
        self.unsafe = True
        self.fail()

    def fail(self) -> None:
        if self.phase != "idle":
            self.phase = "failed"

    def reject(self) -> None:
        self.fail()
        raise _order_error()

    def check_time(self) -> None:
        if self.deadline and asyncio.get_running_loop().time() >= self.deadline:
            self.fail()
            raise _timeout_error()

    def observe(self, conn, _cursor, _sql, _parameters, context, _executemany):
        if conn is not self.connection:
            return
        statement = context.compiled.statement if context.compiled else None
        if self.cleaning and isinstance(statement, RollbackToSavepointClause):
            return
        if self.phase == "failed":
            self.reject()
        if (
            self.permit is not None
            and context.execution_options.get("_authorization_plan_permit")
            is self.permit
        ):
            self.check_time()
            return
        if self.control and isinstance(
            statement,
            (SavepointClause, ReleaseSavepointClause, RollbackToSavepointClause),
        ):
            return
        safe = _known_read(statement)
        if self.phase == "idle":
            self.unsafe |= not safe
            return
        if self.phase != "active" or not safe:
            self.reject()
        self.check_time()

    def after_begin(self, _session, _transaction, connection):
        if self.connection is not None:
            if self.connection is not connection:
                self.unsafe = True
                self.reject()
            return
        self.connection = connection
        event.listen(connection, "before_cursor_execute", self.observe)
        event.listen(connection, "commit", self.connection_commit)
        event.listen(connection, "rollback", self.connection_rollback)
        event.listen(connection, "savepoint", self.connection_savepoint)
        event.listen(connection, "release_savepoint", self.connection_savepoint)
        event.listen(
            connection, "rollback_savepoint", self.connection_rollback_savepoint
        )

    def transaction_created(self, _session, transaction):
        if transaction.parent is None:
            self.outer = transaction
        elif transaction.nested and self.phase != "idle" and not self.control:
            self.reject()

    def transaction_ended(self, _session, transaction):
        if transaction is self.outer:
            self.outer = None
            self.connection = None
            self.handle = None
            self.phase = "idle"
            self.unsafe = False
            self.permit = None
            self.control = 0
            self.committing = False
            self.deadline = 0.0

    def before_flush(self, _session, _flush_context, _instances):
        self.unsafe = True
        if self.phase != "idle":
            self.reject()

    def before_commit(self, session):
        if self.phase == "idle":
            return
        if self.control and self.phase == "active":
            return
        if self.phase != "closed":
            self.reject()
        self.check_time()
        if session.new or session.dirty or session.deleted:
            self.reject()
        self.committing = True

    def connection_commit(self, connection):
        if connection is self.connection and self.phase != "idle":
            if not self.committing:
                self.reject()

    def connection_rollback(self, connection):
        if connection is self.connection:
            self.fail()

    def connection_savepoint(self, connection, *_args):
        if connection is self.connection and self.phase != "idle":
            if not self.control:
                self.reject()

    def connection_rollback_savepoint(self, connection, *_args):
        if not self.cleaning:
            self.connection_savepoint(connection)


class _ObservedConnection:
    """Narrow public connection facade: no unobserved driver/sync callbacks."""

    def __init__(self, connection, db):
        self._connection = connection
        self._db = db

    async def execute(self, *args, **kwargs):
        return await self._db._observed_io(self._connection.execute, *args, **kwargs)

    async def scalar(self, *args, **kwargs):
        return await self._db._observed_io(self._connection.scalar, *args, **kwargs)

    async def scalars(self, *args, **kwargs):
        return await self._db._observed_io(self._connection.scalars, *args, **kwargs)

    async def exec_driver_sql(self, *args, **kwargs):
        return await self._db._observed_io(
            self._connection.exec_driver_sql, *args, **kwargs
        )

    def _reject_escape(self):
        self._db._authorization_journal.unsafe = True
        self._db._authorization_journal.reject()

    @property
    def sync_connection(self):
        self._reject_escape()

    async def get_raw_connection(self):
        self._reject_escape()

    async def run_sync(self, *_args, **_kwargs):
        self._reject_escape()

    async def commit(self):
        self._reject_escape()

    async def rollback(self):
        self._reject_escape()

    async def invalidate(self, *_args):
        self._reject_escape()

    async def close(self):
        self._reject_escape()


class _ObservedTransaction:
    """Transaction metadata without a second owner-commit entry point."""

    def __init__(self, transaction, journal):
        self._transaction = transaction
        self._journal = journal

    @property
    def is_active(self):
        return self._transaction.is_active

    @property
    def nested(self):
        return self._transaction.nested

    async def commit(self):
        self._journal.reject()

    async def rollback(self):
        self._journal.reject()

    async def start(self, *_args, **_kwargs):
        self._journal.reject()

    async def __aenter__(self):
        self._journal.reject()

    async def __aexit__(self, *_args):
        self._journal.reject()


class AuthorizationPlanSession(AsyncSession):
    """Create this session before *any* SQL; late upgrading is unsupported."""

    def __init__(self, bind=None, **kwargs):
        super().__init__(bind=bind, **kwargs)
        journal = _Journal(
            self.sync_session,
            isinstance(bind, AsyncEngine) and not kwargs.get("binds"),
        )
        self._authorization_journal = journal
        for name, listener in (
            ("after_begin", journal.after_begin),
            ("after_transaction_create", journal.transaction_created),
            ("after_transaction_end", journal.transaction_ended),
            ("before_flush", journal.before_flush),
            ("before_commit", journal.before_commit),
        ):
            event.listen(self.sync_session, name, listener)

    async def _observed_io(self, method, *args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except BaseException as exc:
            self._authorization_journal.io_failed(exc)
            raise

    def get_transaction(self):
        transaction = super().get_transaction()
        if transaction is None:
            return None
        return _ObservedTransaction(transaction, self._authorization_journal)

    def get_nested_transaction(self):
        transaction = super().get_nested_transaction()
        if transaction is None:
            return None
        return _ObservedTransaction(transaction, self._authorization_journal)

    def begin(self):
        self._authorization_journal.reject()

    def begin_nested(self):
        self._authorization_journal.reject()

    async def merge(self, *_args, **_kwargs):
        self._authorization_journal.reject()

    async def delete(self, *_args, **_kwargs):
        self._authorization_journal.reject()

    async def reset(self):
        self._authorization_journal.reject()

    async def invalidate(self):
        self._authorization_journal.reject()

    async def connection(self, *args, **kwargs):
        connection = await self._observed_io(super().connection, *args, **kwargs)
        return _ObservedConnection(connection, self)

    async def execute(self, *args, **kwargs):
        return await self._observed_io(super().execute, *args, **kwargs)

    async def scalar(self, *args, **kwargs):
        return await self._observed_io(super().scalar, *args, **kwargs)

    async def scalars(self, *args, **kwargs):
        return await self._observed_io(super().scalars, *args, **kwargs)

    async def get(self, *args, **kwargs):
        return await self._observed_io(super().get, *args, **kwargs)

    async def get_one(self, *args, **kwargs):
        return await self._observed_io(super().get_one, *args, **kwargs)

    async def refresh(self, *args, **kwargs):
        return await self._observed_io(super().refresh, *args, **kwargs)

    async def flush(self, *args, **kwargs):
        return await self._observed_io(super().flush, *args, **kwargs)

    async def commit(self):
        # SQLAlchemy can skip before_commit when a transaction is already
        # PREPARED (for example after a failed SAVEPOINT release).
        if self._authorization_journal.phase not in {"idle", "closed"}:
            self._authorization_journal.reject()
        try:
            return await self._observed_io(super().commit)
        except BaseException:
            self._authorization_journal.phase = "failed"
            raise
        finally:
            self._authorization_journal.committing = False

    async def rollback(self):
        journal = self._authorization_journal
        journal.cleaning = True
        try:
            await super().rollback()
            if not self.in_transaction():
                journal.transaction_ended(self.sync_session, journal.outer)
        finally:
            journal.cleaning = False

    async def close(self):
        journal = self._authorization_journal
        journal.cleaning = True
        try:
            await super().close()
            if not self.in_transaction():
                journal.transaction_ended(self.sync_session, journal.outer)
        finally:
            journal.cleaning = False

    async def run_sync(self, *_args, **_kwargs):
        self._authorization_journal.unsafe = True
        self._authorization_journal.reject()

    async def stream(self, *_args, **_kwargs):
        # The scope's bounded reads must finish inside the observed await.
        self._authorization_journal.reject()

    async def stream_scalars(self, *_args, **_kwargs):
        self._authorization_journal.reject()
