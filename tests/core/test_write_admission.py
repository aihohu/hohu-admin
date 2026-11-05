"""Draining is irreversible within a process and includes all HTTP methods."""

import asyncio

import pytest

import app.core.write_admission as admission_module
from app.core.exceptions import BusinessException
from app.core.scheduler import SchedulerManager
from app.core.write_admission import WriteAdmission
from app.main import app
from app.middleware.write_admission import WriteAdmissionMiddleware
from app.modules.ai.lifecycle import cleanup_orphaned_pending_on_startup
from app.modules.job.job_runner import execute_job, run_job_manual
from app.tasks.user_cleanup_tasks import clean_expired_import_previews


async def test_persistent_stop_refuses_fresh_entry_and_cannot_reopen(tmp_path):
    gate = WriteAdmission(tmp_path)
    async with gate.entry():
        assert gate.active == 1
    (tmp_path / "stop.json").write_text("{}")
    with pytest.raises(BusinessException, match="维护"):
        async with gate.entry():
            pytest.fail("stop marker allowed entry")
    (tmp_path / "stop.json").unlink()
    with pytest.raises(BusinessException):
        async with gate.entry():
            pytest.fail("same process reopened")


async def test_drain_waits_for_existing_work_and_refuses_new_children(tmp_path):
    gate = WriteAdmission(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def writer():
        async with gate.entry():
            entered.set()
            await release.wait()

    task = asyncio.create_task(writer())
    await entered.wait()
    gate.close()
    with pytest.raises(TimeoutError):
        await gate.drain(limit_seconds=0.01)
    assert gate.active == 1
    with pytest.raises(BusinessException):
        async with gate.entry():
            pytest.fail("entry during drain")
    release.set()
    await task
    await gate.drain(limit_seconds=1)
    assert gate.active == 0


async def test_nested_entry_does_not_hide_child_work(tmp_path):
    gate = WriteAdmission(tmp_path)
    async with gate.entry():
        async with gate.entry():
            assert gate.active == 1
        gate.close()
        # Existing admitted work may finish its own phases, without new task admission.
        async with gate.entry():
            assert gate.active == 1

        async def child():
            async with gate.entry():
                pytest.fail("inherited admission")

        with pytest.raises(BusinessException):
            await asyncio.create_task(child())
    assert gate.active == 0


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
async def test_old_web_cannot_bypass_server_stop_with_headers(tmp_path, method):
    gate = WriteAdmission(tmp_path)
    gate.close()
    messages = []

    async def application(_scope, _receive, _send):
        pytest.fail("application reached while stopped")

    async def send(message):
        messages.append(message)

    middleware = WriteAdmissionMiddleware(application, gate=gate)
    await middleware(
        {
            "type": "http",
            "method": method,
            "path": "/system/user",
            "headers": [(b"x-client-version", b"0.2.0")],
        },
        None,
        send,
    )
    assert messages[0]["status"] == 503
    assert b"AUTHORIZATION_MAINTENANCE_ACTIVE" in messages[1]["body"]


async def test_cancelled_request_releases_admission(tmp_path):
    gate = WriteAdmission(tmp_path)
    started = asyncio.Event()

    async def application(_scope, _receive, _send):
        started.set()
        await asyncio.Event().wait()

    middleware = WriteAdmissionMiddleware(application, gate=gate)
    task = asyncio.create_task(
        middleware({"type": "http", "path": "/ai/chat", "method": "POST"}, None, None)
    )
    await started.wait()
    assert gate.active == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.active == 0


async def test_actual_background_entries_refuse_reentry(tmp_path, monkeypatch):

    gate = WriteAdmission(tmp_path)
    gate.close()
    monkeypatch.setattr(admission_module, "write_admission", gate)
    for operation in (
        lambda: execute_job(0, 1),
        lambda: run_job_manual(0, 1),
        clean_expired_import_previews,
        lambda: cleanup_orphaned_pending_on_startup(platform=None),
    ):
        with pytest.raises(BusinessException) as caught:
            await operation()
        assert caught.value.error_code == "AUTHORIZATION_MAINTENANCE_ACTIVE"
    with pytest.raises(BusinessException):
        SchedulerManager().start()


def test_main_installs_admission_outside_audit_and_other_writers():
    assert app.user_middleware[0].cls is WriteAdmissionMiddleware
