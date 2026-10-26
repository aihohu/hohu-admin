"""Bounded external workers for explicit authorization candidate owners.

The capacity lease belongs to the concurrent future, never its async waiter.
Timing out cannot stop a thread or establish whether SMTP already accepted mail.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import BoundedSemaphore

from app.core import security
from app.core.exceptions import BusinessException


class BoundedWorker:
    def __init__(self, *, workers=4, queue=32, timeout=3.0):
        if workers < 1 or queue < 0 or timeout <= 0:
            raise ValueError("Invalid worker budget")
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="authorization-io"
        )
        self._capacity = BoundedSemaphore(workers + queue)
        self.timeout = timeout

    async def run(self, function, *args, **kwargs):
        if not self._capacity.acquire(blocking=False):
            raise BusinessException(
                503,
                "External worker capacity exhausted",
                error_code="AUTHORIZATION_EXTERNAL_BUSY",
            )
        try:
            future = self._executor.submit(partial(function, *args, **kwargs))
        except BaseException:
            self._capacity.release()
            raise
        future.add_done_callback(lambda _future: self._capacity.release())
        wrapped = asyncio.wrap_future(future)
        # Consume late exceptions even after the caller has timed out/cancelled.
        wrapped.add_done_callback(
            lambda completed: (
                completed.exception() if not completed.cancelled() else None
            )
        )
        try:
            async with asyncio.timeout(self.timeout):
                return await asyncio.shield(wrapped)
        except TimeoutError:
            raise BusinessException(
                503,
                "External work acknowledgement timed out",
                error_code="AUTHORIZATION_EXTERNAL_TIMEOUT",
            ) from None

    def close(self):
        """Drain an isolated worker, principally for deterministic tests."""
        self._executor.shutdown(wait=True)


_password_worker = BoundedWorker()
smtp_worker = BoundedWorker(timeout=15.0)


async def hash_password(password: str) -> str:
    return await _password_worker.run(security.get_password_hash, password)


async def verify_password(password: str, encoded: str) -> bool:
    return await _password_worker.run(security.verify_password, password, encoded)
