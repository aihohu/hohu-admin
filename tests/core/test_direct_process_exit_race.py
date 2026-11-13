"""A failed termination call only counts when its bound kernel handle exited."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.core.exceptions import BusinessException
from app.core.process_identity import ProcessIdentity
from tools.ops import authorization_maintenance as ops


@pytest.mark.parametrize("exits_during_termination", [True, False])
async def test_windows_exit_race_requires_same_handle_exit(
    monkeypatch, exits_during_termination
):
    identity = ProcessIdentity(456, "windows:123")
    state = {"exited": False}
    bound_handle = object()

    def terminate(handle, _code):
        assert handle is bound_handle
        state["exited"] = exits_during_termination
        return False

    def wait(handle, timeout):
        assert handle is bound_handle and timeout == 0
        return 0 if state["exited"] else 258

    kernel = SimpleNamespace(TerminateProcess=terminate, WaitForSingleObject=wait)

    @contextmanager
    def process(pid, *, terminate=False):
        assert pid == identity.pid and terminate
        yield bound_handle, kernel

    monkeypatch.setattr(ops, "os", SimpleNamespace(name="nt", getpid=lambda: 123))
    monkeypatch.setattr(
        ProcessIdentity,
        "read",
        staticmethod(lambda _pid: None if state["exited"] else identity),
    )
    monkeypatch.setattr(ops, "windows_process", process)
    monkeypatch.setattr(ops, "windows_birth", lambda _handle, _kernel: identity.birth)
    processes = ops.DirectProcesses((identity,))
    if exits_during_termination:
        await processes.stop()
        assert await processes.stopped()
    else:
        with pytest.raises(BusinessException) as failure:
            await processes.stop()
        assert failure.value.error_code == "AUTHORIZATION_MAINTENANCE_FENCE_REQUIRED"
