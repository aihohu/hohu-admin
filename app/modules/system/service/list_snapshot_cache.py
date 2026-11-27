"""Worker-local, bounded reuse of immutable, verified list-policy facts."""

import asyncio
import os
from collections import OrderedDict
from threading import local
from weakref import WeakValueDictionary


class _Entries:
    def __init__(self, owner):
        self.owner = owner
        self.values = OrderedDict()
        self.locks = WeakValueDictionary()
        self.units = 0


class SnapshotCache:
    def __init__(self, max_entries=8, max_units=8192):
        self.max_entries = max_entries
        self.max_units = max_units
        self.clear()

    def clear(self):
        # A pending loader may finish against its detached generation.
        self._local = local()

    async def get(self, key, load):
        owner = (os.getpid(), asyncio.get_running_loop())
        state = getattr(self._local, "state", None)
        if state is None or state.owner != owner:
            self._local.state = state = _Entries(owner)
        lock = state.locks.get(key)
        if lock is None:
            state.locks[key] = lock = asyncio.Lock()
        async with lock:
            if key in state.values:
                state.values.move_to_end(key)
                return state.values[key][0]
            # The acquiring request owns the session and its transaction deadline.
            facts = await load()
            units = 1 + sum(len(part) for part in facts)
            if units <= self.max_units:
                state.values[key] = (facts, units)
                state.units += units
                while (
                    len(state.values) > self.max_entries or state.units > self.max_units
                ):
                    _, (_, evicted) = state.values.popitem(last=False)
                    state.units -= evicted
            return facts
