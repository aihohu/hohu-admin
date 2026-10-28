"""Real PostgreSQL admission fencing; all objects belong to this test."""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import BusinessException
from app.db.session import engine
from tools.ops import authorization_maintenance as ops
from tools.ops.authorization_maintenance import (
    DirectProcesses,
    PostgresStop,
    ProcessIdentity,
    StopManifest,
)


@pytest.fixture
async def deployment_database():
    name = "stop_test_" + uuid4().hex
    role = name + "_app"
    password = uuid4().hex
    control = create_async_engine(
        engine.url.set(database="postgres"), poolclass=NullPool
    )
    admin = create_async_engine(engine.url.set(database=name), poolclass=NullPool)
    writer = create_async_engine(
        engine.url.set(database=name, username=role, password=password),
        poolclass=NullPool,
    )
    async with control.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        # Generated ASCII identifiers and hex-only password, never user input.
        await connection.execute(
            text('CREATE ROLE "' + role + '" LOGIN PASSWORD ' + "'" + password + "'")
        )
        await connection.execute(
            text('CREATE DATABASE "' + name + '" OWNER "' + role + '"')
        )
    try:
        yield admin, writer, role, name
    finally:
        await writer.dispose()
        await admin.dispose()
        async with control.connect() as connection:
            connection = await connection.execution_options(
                isolation_level="AUTOCOMMIT"
            )
            await connection.execute(text('DROP DATABASE IF EXISTS "' + name + '"'))
            await connection.execute(text('DROP ROLE IF EXISTS "' + role + '"'))
        await control.dispose()


async def test_database_fence_blocks_restart_and_observes_inflight(
    deployment_database, tmp_path
):
    admin, writer, role, name = deployment_database
    manifest = StopManifest("unit", "a" * 40, "direct", ("api", "scheduler"))
    inflight = await writer.connect()
    await inflight.execute(text("SELECT 1"))
    try:
        async with PostgresStop(admin, tmp_path, manifest, (role,)) as guard:
            await guard.prepare()
            with pytest.raises(Exception) as admin_error:
                async with admin.connect():
                    pytest.fail("new privileged application connection admitted")
            assert getattr(admin_error.value, "sqlstate", None) == "55000"
            with pytest.raises(Exception) as caught:
                async with writer.connect():
                    pytest.fail("new writer admitted")
            assert getattr(caught.value, "sqlstate", None) == "55000"
            with pytest.raises(BusinessException):
                await guard.require_quiescent()
            await inflight.close()
            await guard.require_quiescent()
    finally:
        await inflight.close()
    assert (tmp_path / "stop.json").exists()
    control = create_async_engine(
        admin.url.set(database="postgres"), poolclass=NullPool
    )
    try:
        async with control.connect() as db:
            assert (
                await db.scalar(
                    text("SELECT datallowconn FROM pg_database WHERE datname=:name"),
                    {"name": name},
                )
                is False
            )
    finally:
        await control.dispose()


async def test_superuser_application_cannot_reenter_but_owned_pool_can_work(
    deployment_database, tmp_path
):
    admin, _, _, _ = deployment_database
    manifest = StopManifest("unit", "a" * 40, "direct", ("api", "scheduler"))
    async with PostgresStop(admin, tmp_path, manifest, (admin.url.username,)) as guard:
        await guard.prepare()
        with pytest.raises(Exception) as caught:
            async with admin.connect():
                pytest.fail("superuser application reentered")
        assert getattr(caught.value, "sqlstate", None) == "55000"
        async with guard.runtime_engine.connect() as db:
            assert await db.scalar(text("SELECT 1")) == 1
            with pytest.raises(BusinessException):
                await guard.require_quiescent()
        await guard.require_quiescent()


async def test_observation_requires_stopped_process_and_closed_database(
    deployment_database, tmp_path
):
    admin, writer, role, _ = deployment_database
    manifest = StopManifest("unit", "a" * 40, "direct", ("api", "scheduler"))
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(120)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        processes = DirectProcesses((ProcessIdentity.read(child.pid),))
        async with PostgresStop(admin, tmp_path, manifest, (role,)) as guard:
            await guard.prepare(processes)
            with pytest.raises(BusinessException):
                await guard.observe(processes)
            await processes.stop()
            evidence = await guard.observe(processes)
            assert evidence.active_writer_count == 0
            assert evidence.ingress_fenced is True
            assert evidence.stopped_surfaces == {
                "api",
                "worker",
                "scheduler",
                "web-write-ingress",
            }
            with pytest.raises(BusinessException):
                await guard.resume(
                    processes
                )  # A live 30-second proof must not be revoked.
            assert (tmp_path / "stop.json").exists()
    finally:
        if child.returncode is None:
            child.terminate()
        async with asyncio.timeout(10):
            await child.wait()


async def test_new_controller_can_explicitly_resume_after_proof_expires(
    deployment_database, tmp_path
):
    admin, writer, role, _ = deployment_database
    manifest = StopManifest("unit", "a" * 40, "direct", ("api", "scheduler"))
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(120)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    processes = DirectProcesses((ProcessIdentity.read(child.pid),))
    try:
        async with PostgresStop(admin, tmp_path, manifest, (role,)) as guard:
            await guard.prepare(processes)
            await processes.stop()
            await guard.observe(processes)
        # Move only the test evidence clock; no deployed controller is resumed early.
        record = ops.read_object(tmp_path / "stop.json")
        record["observed_at"] = (datetime.now(UTC) - timedelta(seconds=31)).isoformat()
        (tmp_path / "stop.json").write_text(json.dumps(record))
        async with PostgresStop(admin, tmp_path, manifest, (role,)) as recovery:
            await recovery.resume(processes)
        assert not (tmp_path / "stop.json").exists()
        async with writer.connect() as db:
            assert await db.scalar(text("SELECT 1")) == 1
    finally:
        if child.returncode is None:
            child.terminate()
        async with asyncio.timeout(10):
            await child.wait()


async def test_canceled_controller_keeps_database_closed_and_releases_control_lock(
    deployment_database, tmp_path
):
    admin, _, role, name = deployment_database
    manifest = StopManifest("unit", "a" * 40, "direct", ("api", "scheduler"))
    ready = asyncio.Event()

    async def maintain():
        async with PostgresStop(admin, tmp_path, manifest, (role,)) as guard:
            await guard.prepare()
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(maintain())
    async with asyncio.timeout(5):
        await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with PostgresStop(admin, tmp_path, manifest, (role,)) as guard:
        assert guard.database == name
        assert guard.original_allow is False
        await guard.require_quiescent()
    assert (tmp_path / "stop.json").exists()
