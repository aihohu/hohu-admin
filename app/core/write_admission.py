"""Process-local admission, backed by the deployment's persistent stop marker.

This is a drain mechanism, not a stopped-writers attestation. The maintenance
observer must additionally verify process identities and database quiescence.
"""

import asyncio
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path

from app.core.config import settings
from app.core.exceptions import BusinessException


def maintenance_active():
    return BusinessException(
        503, "系统维护中，暂不接受新操作", error_code="AUTHORIZATION_MAINTENANCE_ACTIVE"
    )


class WriteAdmission:
    def __init__(self, directory):
        self._directory = directory
        self._closed = False
        self._tasks = {}
        self._waiters = set()

    @property
    def directory(self):
        return Path(self._directory() if callable(self._directory) else self._directory)

    @property
    def active(self):
        return len(self._tasks)

    @property
    def closed(self):
        if not self._closed:
            try:
                (self.directory / "stop.json").stat()
            except FileNotFoundError:
                pass
            except OSError:
                self.close()
            else:
                self.close()
        return self._closed

    def close(self):
        # Removing the marker never reopens a process which has started draining.
        self._closed = True

    def require_open(self):
        if self.closed:
            raise maintenance_active()

    @asynccontextmanager
    async def entry(self):
        task = asyncio.current_task()
        if task is None:
            raise maintenance_active()
        if task not in self._tasks:
            self.require_open()
        self._tasks[task] = self._tasks.get(task, 0) + 1
        try:
            yield
        finally:
            depth = self._tasks[task] - 1
            if depth:
                self._tasks[task] = depth
            else:
                del self._tasks[task]
            if not self._tasks:
                for waiter in self._waiters:
                    waiter.set()

    async def drain(self, *, limit_seconds=70):
        self.close()
        if asyncio.current_task() in self._tasks:
            raise maintenance_active()
        waiter = asyncio.Event()
        self._waiters.add(waiter)
        try:
            async with asyncio.timeout(limit_seconds):
                if self._tasks:
                    await waiter.wait()
        finally:
            self._waiters.discard(waiter)


write_admission = WriteAdmission(lambda: settings.AUTHORIZATION_CONTROL_DIR)


def require_open():
    write_admission.require_open()


def is_closed():
    return write_admission.closed


def admitted(function):
    """Register an actual background entry; task-local inheritance is forbidden."""

    @wraps(function)
    async def wrapped(*args, **kwargs):
        async with write_admission.entry():
            return await function(*args, **kwargs)

    return wrapped
