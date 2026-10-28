"""Linux process signaling remains generation-bound without Python wrappers."""

import asyncio
import os
import signal
import sys

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
        with process_identity.linux_process(child.pid) as descriptor:
            process_identity.linux_signal(descriptor, signal.SIGTERM)
        async with asyncio.timeout(5):
            await child.wait()
        assert child.returncode == -signal.SIGTERM
    finally:
        if child.returncode is None:
            child.terminate()
            await child.wait()
