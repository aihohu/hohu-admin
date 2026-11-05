"""Cooperative task drain is required, but never used as OS/DB attestation."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import BusinessException
from app.core.process_admission import ProcessAdmission
from app.core.process_identity import ProcessIdentity
from app.core.write_admission import WriteAdmission


async def test_stop_marker_drains_active_work_and_latches_closed(tmp_path):
    gate = WriteAdmission(tmp_path)
    entered, release, stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def active():
        async with gate.entry():
            entered.set()
            await release.wait()

    async def stop_source():
        stopping.set()

    work = asyncio.create_task(active())
    process = ProcessAdmission(
        gate, role="all", build_sha="a" * 40, stop_callbacks=(stop_source,)
    )
    try:
        await entered.wait()
        await process.start()
        marker = tmp_path / "stop.json"
        marker.write_text("{}")
        async with asyncio.timeout(3):
            await stopping.wait()
        assert json.loads(process.path.read_text())["state"] == "draining"
        assert not process.task.done()
        with pytest.raises(BusinessException):
            async with gate.entry():
                pytest.fail("new work admitted")
        release.set()
        await work
        async with asyncio.timeout(3):
            await asyncio.shield(process.task)
        assert json.loads(process.path.read_text())["state"] == "quiesced"
        marker.unlink()
        with pytest.raises(BusinessException):
            gate.require_open()
    finally:
        release.set()
        await work
        await process.stop()


async def test_failed_source_stop_still_attempts_other_sources(tmp_path):
    gate = WriteAdmission(tmp_path)
    first = AsyncMock(side_effect=RuntimeError("failed source"))
    second = AsyncMock()
    process = ProcessAdmission(
        gate, role="api", build_sha="a" * 40, stop_callbacks=(first, second)
    )
    await process.start()
    (tmp_path / "stop.json").write_text("{}")
    try:
        with pytest.raises(RuntimeError):
            async with asyncio.timeout(3):
                await asyncio.shield(process.task)
        assert json.loads(process.path.read_text())["state"] == "unverified"
    finally:
        with pytest.raises(RuntimeError):
            await process.stop()
    second.assert_awaited_once()
    assert gate.closed


def test_shared_compose_directory_does_not_collide_on_container_pid(
    monkeypatch, tmp_path
):
    gate = WriteAdmission(tmp_path)
    monkeypatch.setattr(
        ProcessIdentity,
        "read",
        lambda _pid: ProcessIdentity(1, "linux:boot:10:namespace-a"),
    )
    api = ProcessAdmission(gate, role="api", build_sha="a" * 40)
    monkeypatch.setattr(
        ProcessIdentity,
        "read",
        lambda _pid: ProcessIdentity(1, "linux:boot:10:namespace-b"),
    )
    scheduler = ProcessAdmission(gate, role="scheduler", build_sha="a" * 40)
    api._record("starting")
    scheduler._record("starting")
    assert api.path != scheduler.path
    assert len(list(tmp_path.glob("process-*.json"))) == 2
