"""Linux process signaling remains generation-bound without Python wrappers."""

import asyncio
import errno
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from app.core import process_identity
from app.core.exceptions import BusinessException


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


@pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel interface")
def test_open_stat_read_after_process_exit_requires_kernel_confirmation(monkeypatch):
    read = Path.read_text
    observed_errors = []
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"]
    ) as child:
        target = Path("/proc", str(child.pid), "stat")

        def exit_during_read(path, *args, **kwargs):
            if path != target:
                return read(path, *args, **kwargs)
            # Exercise the real kernel race inside Path.read_text: open succeeds,
            # but the process exits before the descriptor is read.
            with path.open() as stream:
                child.terminate()
                child.wait(timeout=5)
                try:
                    return stream.read()
                except ProcessLookupError as error:
                    observed_errors.append(error.errno)
                    raise

        monkeypatch.setattr(Path, "read_text", exit_during_read)
        try:
            assert process_identity.ProcessIdentity.read(child.pid) is None
            assert observed_errors == [errno.ESRCH]
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel interface")
def test_process_lookup_error_does_not_prove_a_live_process_exited(monkeypatch):
    read = Path.read_text
    target = Path("/proc", str(os.getpid()), "stat")

    def missing(path, *args, **kwargs):
        if path == target:
            raise ProcessLookupError(errno.ESRCH, "process changed during read")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", missing)
    with pytest.raises(BusinessException):
        process_identity.ProcessIdentity.read(os.getpid())


@pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel interface")
def test_process_disappearance_with_unreadable_pidfd_remains_closed(monkeypatch):
    read = Path.read_text
    target = Path("/proc", str(os.getpid()), "stat")

    def missing(path, *args, **kwargs):
        if path == target:
            raise ProcessLookupError(errno.ESRCH, "process changed during read")
        return read(path, *args, **kwargs)

    def denied_pidfd(_pid):
        raise PermissionError(errno.EACCES, "pidfd denied")

    monkeypatch.setattr(Path, "read_text", missing)
    monkeypatch.setattr(process_identity, "linux_process", denied_pidfd)
    with pytest.raises((BusinessException, PermissionError)):
        process_identity.ProcessIdentity.read(os.getpid())


@pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel interface")
@pytest.mark.parametrize(
    "error",
    [
        PermissionError(errno.EACCES, "denied"),
        OSError(errno.EIO, "I/O error"),
        IndexError("short stat"),
    ],
)
def test_unknown_process_read_errors_remain_closed(monkeypatch, error):
    read = Path.read_text
    target = Path("/proc", str(os.getpid()), "stat")

    def unreadable(path, *args, **kwargs):
        if path == target:
            raise error
        return read(path, *args, **kwargs)

    def unexpected_exit_check(_pid):
        pytest.fail("unknown read errors cannot be converted into exit evidence")

    monkeypatch.setattr(Path, "read_text", unreadable)
    monkeypatch.setattr(process_identity, "linux_exited", unexpected_exit_check)
    with pytest.raises(BusinessException):
        process_identity.ProcessIdentity.read(os.getpid())
