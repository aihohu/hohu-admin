"""Observe a specific wait edge on an already connected PostgreSQL session."""

import asyncio

from sqlalchemy import func, select


async def wait_for_blocker(probe, *, holder, waiter, pending):
    assert holder != waiter
    try:
        async with asyncio.timeout(5):
            while not pending.done():
                if holder in await probe.scalar(select(func.pg_blocking_pids(waiter))):
                    assert not pending.done()
                    return
                await asyncio.sleep(0.01)
        await pending  # Propagate a contender failure before reporting a missing lock.
        raise AssertionError(f"PID {waiter} completed without waiting for {holder}")
    except TimeoutError as exc:
        exc.add_note(f"Missing PostgreSQL wait edge: waiter={waiter}, holder={holder}")
        raise
