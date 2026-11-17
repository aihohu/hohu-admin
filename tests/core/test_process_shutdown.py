"""Shutdown must await cleanup even when startup or the served body fails."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import app.main as main
import app.scheduler_worker as worker
from app.core.tenant import PlatformContext
from app.core.write_admission import WriteAdmission


@pytest.mark.parametrize("startup_failure", [False, True])
async def test_lifespan_failure_closes_started_resources(
    monkeypatch, startup_failure, tmp_path
):
    monkeypatch.setattr(main.settings, "AUTHORIZATION_CONTROL_DIR", str(tmp_path))

    @asynccontextmanager
    async def sessions():
        yield object()

    scheduler = SimpleNamespace(
        reload_jobs=AsyncMock(),
        start_with_pubsub=AsyncMock(),
        stop=AsyncMock(),
        shutdown=Mock(),
    )
    monitor = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    if startup_failure:
        monitor.start.side_effect = RuntimeError("startup interrupted")
    monkeypatch.setattr(main.settings, "AI_MODULE_ENABLED", False)
    monkeypatch.setattr(main.settings, "APP_ROLE", "all")
    monkeypatch.setattr(main, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(main, "scheduler_manager", scheduler)
    monkeypatch.setattr(main, "JobLogMonitor", lambda **_kwargs: monitor)
    close = AsyncMock()
    monkeypatch.setattr(main, "close_redis", close)
    with pytest.raises(RuntimeError):
        async with main.lifespan(main.app):
            raise RuntimeError("served body interrupted")
    monitor.stop.assert_awaited_once()
    scheduler.stop.assert_awaited_once()
    close.assert_awaited_once()


@pytest.mark.parametrize("failure", [None, "start", "stop"])
async def test_standalone_worker_owns_recovery_and_all_shutdown_paths(
    monkeypatch, tmp_path, failure
):
    platform = PlatformContext(
        0,
        "scheduler-worker",
        "service",
        frozenset({"platform:job:operate"}),
        "startup",
        "test",
        "test",
    )
    handlers, admissions = {}, []
    monkeypatch.setattr(
        worker.signal,
        "signal",
        lambda sig, callback: handlers.__setitem__(sig, callback),
    )
    monkeypatch.setattr(worker, "write_admission", WriteAdmission(tmp_path))
    original = worker.ProcessAdmission

    def admission(*args, **kwargs):
        process = original(*args, **kwargs)
        admissions.append(process)
        return process

    monkeypatch.setattr(worker, "ProcessAdmission", admission)

    async def run():
        handlers[worker.signal.SIGTERM]()
        return platform

    monkeypatch.setattr(worker, "_run", run)
    monitor = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    if failure:
        getattr(monitor, failure).side_effect = RuntimeError("monitor " + failure)
    factory = Mock(return_value=monitor)
    monkeypatch.setattr(worker, "JobLogMonitor", factory, raising=False)
    stop, close = AsyncMock(), AsyncMock()
    monkeypatch.setattr(worker.scheduler_manager, "stop", stop)
    monkeypatch.setattr(worker, "close_redis", close)
    if failure:
        with pytest.raises(RuntimeError, match="monitor " + failure):
            await worker.main()
    else:
        await worker.main()
    factory.assert_called_once_with(platform=platform, runner_id=worker.RUNNER_ID)
    monitor.start.assert_awaited_once()
    monitor.stop.assert_awaited_once()
    assert monitor.stop in admissions[0].stop_callbacks
    stop.assert_awaited_once()
    close.assert_awaited_once()


async def test_scheduler_cleanup_continues_after_stop_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(worker.settings, "AUTHORIZATION_CONTROL_DIR", str(tmp_path))
    monkeypatch.setattr(worker, "_run", AsyncMock(side_effect=RuntimeError("startup")))
    stop = AsyncMock(side_effect=RuntimeError("scheduler cleanup"))
    monkeypatch.setattr(worker.scheduler_manager, "stop", stop)
    close = AsyncMock()
    monkeypatch.setattr(worker, "close_redis", close)
    monkeypatch.setattr(worker.signal, "signal", Mock())
    with pytest.raises(RuntimeError):
        await worker.main()
    close.assert_awaited_once()
