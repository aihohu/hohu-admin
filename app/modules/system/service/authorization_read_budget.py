"""Bound materialized candidate reads, including ORM select-in relationships."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import event, inspect
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import BindParameter

from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error


def _limit():
    return BusinessException(
        422, "授权事实读取超过预算", error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
    )


def _size(value):
    if value is None:
        return 8
    if isinstance(value, str):
        return 64 + len(value) * 4
    if isinstance(value, bytes):
        return 64 + len(value)
    if isinstance(value, (int, float, bool, date, datetime, Decimal)):
        return 64
    if isinstance(value, dict):
        return 64 + sum(_size(k) + _size(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return 64 + sum(_size(v) for v in value)
    state = inspect(value, raiseerr=False)
    if state is not None and hasattr(state, "mapper"):
        return 256 + sum(
            _size(state.dict.get(column.key)) for column in state.mapper.column_attrs
        )
    # SQLAlchemy Row; never dereference lazy relationships or stringify secrets.
    if hasattr(value, "_mapping"):
        return 64 + sum(_size(v) for v in value)
    raise _order_error()


@dataclass
class AuthorizationReadBudget:
    max_rows: int = 100000
    max_bytes: int = 64 * 1024 * 1024
    rows: int = 0
    decoded_bytes: int = 0
    statements: int = 0

    def observe(self, state):
        if not isinstance(state.statement, Select):
            return state.invoke_statement()
        remaining = self.max_rows - self.rows
        if remaining < 0:
            raise _limit()
        statement = state.statement
        # Never truncate an unknown set: a sentinel row must fail the operation.
        existing = statement._limit_clause
        if existing is None:
            statement = statement.limit(remaining + 1)
        elif isinstance(existing, BindParameter) and type(existing.value) is int:
            statement = statement.limit(min(existing.value, remaining + 1))
        else:
            # Candidate reads use concrete bounded limits, never dynamic SQL limits.
            raise _limit()
        self.statements += 1
        result = state.invoke_statement(statement=statement)
        frozen = result.freeze()
        self.rows += len(frozen.data)
        self.decoded_bytes += sum(_size(row) for row in frozen.data)
        if self.rows > self.max_rows or self.decoded_bytes > self.max_bytes:
            raise _limit()
        return frozen()


@contextmanager
def bounded_authorization_reads(db, budget=None):
    if not isinstance(db, AuthorizationPlanSession):
        raise _order_error()
    budget = budget or AuthorizationReadBudget()
    event.listen(db.sync_session, "do_orm_execute", budget.observe, retval=True)
    try:
        yield budget
    finally:
        event.remove(db.sync_session, "do_orm_execute", budget.observe)
