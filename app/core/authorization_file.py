"""Task-owned limits for necessary synchronous local FILE IO.

Deadlines are cooperative checks around synchronous operations, not an OS IO
timeout. No worker or task outlives the owning transaction to publish bytes.
"""

import asyncio
import time
from contextlib import contextmanager
from contextvars import ContextVar

from app.core.exceptions import BusinessException

_CURRENT = ContextVar("authorization_file_budget", default=None)
_MIB = 1024 * 1024


def _limit():
    return BusinessException(
        422, "文件 IO 超出事务预算", error_code="AUTHORIZATION_FILE_LIMIT_EXCEEDED"
    )


def _order():
    return BusinessException(
        409, "文件事务归属或证明无效", error_code="FIELD_POLICY_LOCK_ORDER"
    )


class FileBudget:
    def __init__(
        self,
        *,
        max_payload_bytes=10 * _MIB,
        max_total_bytes=60 * _MIB,
        max_reads=6,
        deadline=None,
        clock=time.monotonic,
    ):
        for value, maximum in (
            (max_payload_bytes, 10 * _MIB),
            (max_total_bytes, 60 * _MIB),
            (max_reads, 6),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise _limit()
        self.max_payload_bytes = max_payload_bytes
        self.max_total_bytes = max_total_bytes
        self.max_reads = max_reads
        self.deadline = deadline
        self.clock = clock
        self.payload_bytes = self.reads = self.written_bytes = self.writes = 0
        self.manifest_reads = 0
        self._task = None
        self._used = False
        self._failed = False

    def check(self):
        if asyncio.current_task() is not self._task or self._failed:
            raise _order()
        if self.deadline is not None and self.clock() >= self.deadline:
            self._failed = True
            raise BusinessException(
                503,
                "文件阶段事务预算已耗尽",
                error_code="AUTHORIZATION_TRANSACTION_TIMEOUT",
            )

    @contextmanager
    def activate(self):
        if _CURRENT.get() is not None or self._used:
            raise _order()
        self._used = True
        self._task = asyncio.current_task()
        token = _CURRENT.set(self)
        try:
            self.check()
            yield self
            self.check()
        finally:
            _CURRENT.reset(token)

    def read(self, path, *, manifest=False):
        try:
            return self._read(path, manifest=manifest)
        except BaseException:
            self._failed = True
            raise

    def _read(self, path, *, manifest):
        self.check()
        if manifest:
            if self.manifest_reads >= 16:
                raise _limit()
            self.manifest_reads += 1
            maximum = 16 * 1024
        else:
            if self.reads >= self.max_reads:
                raise _limit()
            maximum = min(
                self.max_payload_bytes, self.max_total_bytes - self.payload_bytes
            )
        # The extra byte is a bounded overflow sentinel, never an unbounded read.
        with path.open("rb") as source:
            value = source.read(maximum + 1)
        if not manifest:
            self.reads += 1
            self.payload_bytes += len(value)
        if len(value) > maximum:
            raise _limit()
        self.check()
        return value

    def writing(self, data):
        self.check()
        if (
            type(data) is not bytes
            or len(data) > self.max_payload_bytes
            or self.writes >= 4
            or self.written_bytes + len(data) > 40 * _MIB
        ):
            self._failed = True
            raise _limit()
        self.writes += 1
        self.written_bytes += len(data)


def read_local(path, *, manifest=False):
    budget = _CURRENT.get()
    return budget.read(path, manifest=manifest) if budget else path.read_bytes()


def read_local_bounded(path, maximum):
    """Retain legacy bounded reads while charging planned upload reads."""
    budget = _CURRENT.get()
    if budget:
        return budget.read(path)
    with path.open("rb") as source:
        return source.read(maximum + 1)


def require_unbudgeted_legacy_write():
    budget = _CURRENT.get()
    if budget is not None:
        budget._failed = True
        raise _order()


@contextmanager
def local_publication(data=None):
    budget = _CURRENT.get()
    if budget:
        budget.check()
        if data is not None:
            budget.writing(data)
    try:
        yield
    except BaseException:
        if budget:
            budget._failed = True
        raise
    else:
        if budget:
            budget.check()
