"""Bounded external work keeps capacity until a timed-out worker actually exits."""

import asyncio
import threading

import pytest

from app.core.authorization_io import BoundedWorker, hash_password, verify_password
from app.core.exceptions import BusinessException


@pytest.mark.asyncio
async def test_password_worker_preserves_hash_strength():
    encoded = await hash_password("CandidateStrong42!")
    assert await verify_password("CandidateStrong42!", encoded)
    assert not await verify_password("DifferentStrong42!", encoded)


@pytest.mark.asyncio
async def test_timeout_and_cancellation_do_not_release_running_capacity():
    worker = BoundedWorker(workers=1, queue=0, timeout=0.02)
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        release.wait(2)

    try:
        with pytest.raises(BusinessException) as error:
            await worker.run(blocked)
        assert entered.is_set()
        assert error.value.error_code == "AUTHORIZATION_EXTERNAL_TIMEOUT"
        with pytest.raises(BusinessException) as error:
            await worker.run(lambda: None)
        assert error.value.error_code == "AUTHORIZATION_EXTERNAL_BUSY"
    finally:
        release.set()
        await asyncio.to_thread(worker.close)


@pytest.mark.asyncio
async def test_queue_bound_and_cancelled_waiter_retain_capacity():
    worker = BoundedWorker(workers=1, queue=1, timeout=2)
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        release.wait(2)

    first = asyncio.create_task(worker.run(blocked))
    await asyncio.to_thread(entered.wait, 1)
    second = asyncio.create_task(worker.run(lambda: "queued"))
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    try:
        with pytest.raises(BusinessException) as error:
            await worker.run(lambda: None)
        assert error.value.error_code == "AUTHORIZATION_EXTERNAL_BUSY"
    finally:
        release.set()
        await first
        await asyncio.to_thread(worker.close)
