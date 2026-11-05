"""Kernel process generation checks; PID numbers alone are not identities."""

import ctypes
import os
import select
import signal
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.core.authorization_maintenance import _required


def _libc_pidfd(name, arguments):
    # Some standalone CPython builds omit the wrappers despite a supporting
    # host kernel/libc. Use the same pidfd ABI; never fall back to kill(pid).
    try:
        function = getattr(ctypes.CDLL(None, use_errno=True), name)
    except (AttributeError, OSError):
        raise _required() from None
    function.argtypes = arguments
    function.restype = ctypes.c_int
    return function


@contextmanager
def linux_process(pid):
    if hasattr(os, "pidfd_open"):
        descriptor = os.pidfd_open(pid)
    else:
        descriptor = _libc_pidfd("pidfd_open", [ctypes.c_int, ctypes.c_uint])(pid, 0)
        if descriptor < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def linux_signal(descriptor, sig):
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(descriptor, sig)
    elif (
        _libc_pidfd(
            "pidfd_send_signal",
            [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint],
        )(descriptor, sig, None, 0)
        < 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def linux_exited(pid):
    try:
        with linux_process(pid) as descriptor:
            poll = select.poll()
            poll.register(descriptor, select.POLLIN)
            events = poll.poll(0)
            if not events:
                return False
            if events[0][1] & (select.POLLIN | select.POLLHUP):
                return True
            raise _required()
    except ProcessLookupError:
        return True


@contextmanager
def windows_process(pid, *, terminate=False):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    handle = kernel.OpenProcess(0x100000 | 0x1000 | (1 if terminate else 0), False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # No such process; access denied is unknown.
            yield None, kernel
            return
        raise _required()
    try:
        yield handle, kernel
    finally:
        kernel.CloseHandle(handle)


def windows_birth(handle, kernel):
    if handle is None or kernel.WaitForSingleObject(handle, 0) == 0:
        return None
    times = [ctypes.c_ulonglong() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        raise _required()
    return "windows:" + str(times[0].value)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    birth: str

    @classmethod
    def read(cls, pid):
        if type(pid) is not int or pid <= 0:
            raise _required()
        if os.name == "nt":
            with windows_process(pid) as (handle, kernel):
                birth = windows_birth(handle, kernel)
            return cls(pid, birth) if birth else None
        if os.name != "posix" or not Path("/proc/sys/kernel/random/boot_id").is_file():
            raise _required()
        try:
            fields = (
                Path("/proc", str(pid), "stat").read_text().rpartition(")")[2].split()
            )
            # A zombie leader can still have running threads. Only a readable
            # process pidfd proves the entire thread group has terminated.
            if fields[0] == "Z" and linux_exited(pid):
                return None
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            namespace = Path("/proc", str(pid), "ns/pid").stat().st_ino
            return cls(pid, "linux:" + boot + ":" + fields[19] + ":" + str(namespace))
        except FileNotFoundError:
            if linux_exited(pid):
                return None
            raise _required() from None
        except (OSError, IndexError):
            raise _required() from None
