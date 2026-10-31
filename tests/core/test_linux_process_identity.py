"""Linux process signaling remains generation-bound without Python wrappers."""

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from app.core import process_identity


@pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel interface")
async def test_libc_pidfd_fallback_stops_only_the_opened_process(monkeypatch):
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(120)"
    )
    try:
        assert process_identity.ProcessIdentity.read(child.pid) is not None
        with process_identity.linux_process(child.pid) as descriptor:
            process_identity.linux_signal(descriptor, signal.SIGTERM)
        async with asyncio.timeout(5):
            await child.wait()
        assert child.returncode == -signal.SIGTERM
        assert process_identity.ProcessIdentity.read(child.pid) is None
    finally:
        if child.returncode is None:
            child.terminate()
            await child.wait()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel interface")
async def test_zombie_leader_text_is_not_proof_all_threads_stopped(monkeypatch):
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(120)"
    )
    read = Path.read_text

    def leader_zombie(path, *args, **kwargs):
        value = read(path, *args, **kwargs)
        if path == Path("/proc", str(child.pid), "stat"):
            head, separator, tail = value.rpartition(")")
            return head + separator + " Z " + tail.split(maxsplit=1)[1]
        return value

    try:
        monkeypatch.setattr(Path, "read_text", leader_zombie)
        assert process_identity.ProcessIdentity.read(child.pid) is not None
    finally:
        if child.returncode is None:
            child.terminate()
        await child.wait()
