"""A stop signal alone is never an observation of stopped writers."""

import asyncio
import json
import sys

import pytest

from app.core.exceptions import BusinessException
from tools.ops.authorization_maintenance import (
    ComposeProcesses,
    DirectProcesses,
    ProcessIdentity,
    StopManifest,
)


async def test_direct_observer_checks_real_process_generation():
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(120)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        identity = ProcessIdentity.read(child.pid)
        assert identity is not None
        observer = DirectProcesses((identity,))
        assert not await observer.stopped()
        wrong = DirectProcesses(
            (ProcessIdentity(identity.pid, identity.birth + "wrong"),)
        )
        with pytest.raises(BusinessException):
            await wrong.stop()
        assert child.returncode is None
        await observer.stop()
        assert await observer.stopped()
    finally:
        if child.returncode is None:
            child.terminate()
        async with asyncio.timeout(10):
            await child.wait()


async def test_no_processes_is_not_a_complete_deployment():
    with pytest.raises(BusinessException):
        DirectProcesses(())


def test_stop_manifest_is_persistent_and_never_overwritten(tmp_path):

    manifest = StopManifest("unit", "a" * 40, "direct", ("api", "scheduler"))
    manifest.persist(tmp_path)
    value = json.loads((tmp_path / "stop.json").read_text())
    assert value["build_sha"] == "a" * 40
    with pytest.raises(FileExistsError):
        manifest.persist(tmp_path)


async def test_compose_does_not_treat_stop_command_as_evidence(monkeypatch):

    observer = ComposeProcesses("unit", {"api": ("a" * 64,), "scheduler": ("b" * 64,)})
    state = {"running": True, "restart": "always"}

    async def docker(*args):
        if args[0] == "ps":
            return "a" * 64 + "\n" + "b" * 64
        if args[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Id": cid,
                        "Config": {
                            "Labels": {
                                "com.docker.compose.project": "unit",
                                "com.docker.compose.service": service,
                            }
                        },
                        "State": {
                            "Running": state["running"],
                            "Restarting": False,
                            "Paused": False,
                            "Dead": False,
                        },
                        "HostConfig": {"RestartPolicy": {"Name": state["restart"]}},
                    }
                    for service, ids in observer.services.items()
                    for cid in ids
                ]
            )
        if args[0] == "update":
            state["restart"] = "no"
        return ""

    monkeypatch.setattr(observer, "_docker", docker)
    await observer.stop()
    assert not await observer.stopped()  # Docker returned success but still running.
    state["running"] = False
    assert await observer.stopped()
    state["restart"] = "always"
    assert not await observer.stopped()
