"""Local process registration and cooperative background drain for both launch modes."""

import asyncio
import hashlib
import json
import os
from contextlib import AsyncExitStack
from dataclasses import asdict

from app.core.process_identity import ProcessIdentity


class ProcessAdmission:
    def __init__(self, gate, *, role, build_sha, stop_callbacks=()):
        self.gate, self.role, self.build_sha = gate, role, build_sha
        self.stop_callbacks = list(stop_callbacks)
        self.identity = ProcessIdentity.read(os.getpid())
        generation = hashlib.sha256(self.identity.birth.encode()).hexdigest()[:24]
        self.path = gate.directory / (
            "process-" + str(self.identity.pid) + "-" + generation + ".json"
        )
        self.task = None

    def _record(self, state):
        self.gate.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(
                {
                    "identity": asdict(self.identity),
                    "role": self.role,
                    "build_sha": self.build_sha,
                    "state": state,
                    "active_tasks": self.gate.active,
                },
                output,
            )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)

    async def start(self):
        self.gate.require_open()
        self._record("starting")
        self.gate.require_open()
        self.task = asyncio.create_task(self._watch())

    async def _watch(self):
        while not self.gate.closed:  # noqa: ASYNC110 - another process creates the marker
            await asyncio.sleep(0.25)
        self._record("draining")
        try:
            async with asyncio.timeout(75):
                cleanup = AsyncExitStack()
                for callback in reversed(tuple(self.stop_callbacks)):
                    cleanup.push_async_callback(callback)
                await cleanup.aclose()
                await self.gate.drain(limit_seconds=70)
            self._record("quiesced")
        except BaseException:
            self._record("unverified")
            raise

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            finally:
                self.task = None
                self._record("exiting")
