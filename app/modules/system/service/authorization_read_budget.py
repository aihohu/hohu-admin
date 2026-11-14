"""Bound materialized candidate reads, including ORM select-in relationships."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import wraps

from sqlalchemy import event, inspect
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import BindParameter

from app.core.authorization_maintenance import require_maintenance
from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error

RELATION_READ_PHASE_LIMITS = {
    "barrier": 64,
    "identity": 128,
    "manifest": 1030,
    "scope": 1024,
    "proof": 256,
    "projection": 256,
    "source": 66,
    "delegation": 1024,
}
_read_phase = ContextVar("authorization_read_phase", default=None)


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
    max_statements: int | None = None
    phase_limits: dict[str, int] | None = None
    phase_statements: dict[str, int] = field(default_factory=dict)
    phase_rows: dict[str, int] = field(default_factory=dict)
    phase_bytes: dict[str, int] = field(default_factory=dict)

    def observe(self, state):
        if not isinstance(state.statement, Select):
            return state.invoke_statement()
        current = _read_phase.get()
        phase = (
            current[2]
            if current and current[0].sync_session is state.session
            else "unclassified"
        )
        used = self.phase_statements.get(phase, 0)
        if self.phase_limits is not None and (
            phase not in self.phase_limits or used >= self.phase_limits[phase]
        ):
            raise _limit()
        if self.max_statements is not None and (
            type(self.max_statements) is not int
            or self.max_statements < 0
            or self.statements >= self.max_statements
        ):
            raise _limit()
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
        self.phase_statements[phase] = used + 1
        result = state.invoke_statement(statement=statement)
        frozen = result.freeze()
        rows = len(frozen.data)
        decoded = sum(_size(row) for row in frozen.data)
        self.rows += rows
        self.decoded_bytes += decoded
        self.phase_rows[phase] = self.phase_rows.get(phase, 0) + rows
        self.phase_bytes[phase] = self.phase_bytes.get(phase, 0) + decoded
        if self.rows > self.max_rows or self.decoded_bytes > self.max_bytes:
            raise _limit()
        return frozen()


@contextmanager
def bounded_authorization_reads(db, budget=None):
    if not isinstance(db, AuthorizationPlanSession):
        raise _order_error()
    budget = budget or AuthorizationReadBudget()
    previous = _read_phase.get()
    phase = previous[2] if previous and previous[0] is db else "barrier"
    token = _read_phase.set((db, budget, phase))
    event.listen(db.sync_session, "do_orm_execute", budget.observe, retval=True)
    try:
        yield budget
    finally:
        event.remove(db.sync_session, "do_orm_execute", budget.observe)
        _read_phase.reset(token)


@contextmanager
def bounded_maintenance_reads(db, fence):
    """A verified stopped-writer migration can bound its ordinary SQL session."""
    require_maintenance(fence, "migration")
    budget = AuthorizationReadBudget(max_statements=8192)
    token = _read_phase.set((db, budget, "barrier"))
    event.listen(db.sync_session, "do_orm_execute", budget.observe, retval=True)
    try:
        yield budget
    finally:
        event.remove(db.sync_session, "do_orm_execute", budget.observe)
        _read_phase.reset(token)


@contextmanager
def authorization_read_phase(db, phase):
    current = _read_phase.get()
    if not current or current[0] is not db or phase not in RELATION_READ_PHASE_LIMITS:
        raise _order_error()
    token = _read_phase.set((db, current[1], phase))
    try:
        yield
    finally:
        _read_phase.reset(token)


def read_phase(phase, *, session_arg=0, preserve=()):
    """Instrument a read helper without replacing its session/lock proof."""

    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            db = args[session_arg] if len(args) > session_arg else kwargs["db"]
            current = _read_phase.get()
            if current is None:
                return await function(*args, **kwargs)
            selected = current[2] if current[2] in preserve else phase
            with authorization_read_phase(db, selected):
                return await function(*args, **kwargs)

        return wrapped

    return decorate
