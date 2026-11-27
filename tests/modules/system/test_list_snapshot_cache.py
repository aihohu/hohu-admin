import asyncio

import pytest

from app.modules.system.service.list_snapshot_cache import SnapshotCache


async def test_cold_read_coalesces_and_cancelled_loader_can_retry():
    cache = SnapshotCache()
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return (("grant",), (), frozenset())

    first = asyncio.create_task(cache.get("key", load))
    await started.wait()
    followers = [asyncio.create_task(cache.get("key", load)) for _ in range(6)]
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert len(await asyncio.gather(*followers)) == 6 and calls == 2


async def test_capacity_evicts_lru_and_skips_oversize():
    cache = SnapshotCache(max_entries=2, max_units=4)
    calls = []

    async def read(key, size=1):
        async def load():
            calls.append(key)
            return ((None,) * size, (), frozenset())

        await cache.get(key, load)

    for key in "abacb":
        await read(key)
    assert calls == list("abcb")
    await read("large", 5)
    await read("large", 5)
    assert calls[-2:] == ["large", "large"]
    cache.clear()
    await read("b")
    assert calls[-1] == "b"


async def test_weight_limit_and_reset_during_load():
    cache = SnapshotCache(max_entries=8, max_units=4)
    entered, release = asyncio.Event(), asyncio.Event()
    facts = ((1, 2), (), frozenset())
    calls = []

    async def pending():
        entered.set()
        await release.wait()
        return ((), (), frozenset())

    async def load():
        calls.append(1)
        return facts

    old = asyncio.create_task(cache.get("a", pending))
    await entered.wait()
    cache.clear()
    assert await cache.get("a", load) == facts
    release.set()
    await old
    assert await cache.get("a", load) == facts and len(calls) == 1
    await cache.get("b", load)
    await cache.get("a", load)
    assert len(calls) == 3
