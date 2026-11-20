"""Real migration and seed on fenced connections, never a mocked database."""

import asyncio
import json

import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.core.exceptions import BusinessException
from tests.scripts.test_authorization_maintenance_database import (
    deployment_database as deployment_database,
)
from tools.ops import controlled_install as install


def options(tmp_path, role, monkeypatch, admin, *, bootstrap):
    value = {
        "manifest": {
            "deployment_id": "install-test",
            "build_sha": "a" * 40,
            "mode": "direct",
            "surfaces": ["api", "scheduler"],
        },
        "application_roles": [role],
    }
    if bootstrap:
        value["bootstrap"] = True
    else:
        value["processes"] = {
            "mode": "direct",
            "identities": [{"pid": 987654321, "birth": "windows:123"}],
        }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(install.ops.settings, "RELEASE_BUILD_SHA", "a" * 40)
    monkeypatch.setenv(
        "AUTHORIZATION_MAINTENANCE_DATABASE_URL",
        admin.url.render_as_string(hide_password=False),
    )
    monkeypatch.setenv("AUTHORIZATION_MAINTENANCE_PASSWORD", "Install-Test-123!")
    args = ["prepare", "--inventory", str(path), "--control-dir", str(tmp_path)]
    if bootstrap:
        args.append("--bootstrap")
    return install.arguments(args)


async def assert_closed(admin, writer, tmp_path):
    assert (tmp_path / "stop.json").exists()
    for engine in (admin, writer):
        with pytest.raises(Exception) as error:
            async with engine.connect():
                pytest.fail("new login admitted during controlled installation")
        assert getattr(error.value, "sqlstate", None) == "55000"


@pytest.mark.parametrize("bootstrap", [True, False])
async def test_install_and_explicit_resume_preserve_data_and_application_access(
    deployment_database, tmp_path, monkeypatch, bootstrap
):
    admin, writer, role, _ = deployment_database
    if not bootstrap:

        def old_schema(connection):
            config = Config()
            config.set_main_option("script_location", str(install.ROOT / "alembic"))
            config.set_main_option(
                "sqlalchemy.url", "postgresql+asyncpg://unused/unused"
            )
            config.attributes["connection"] = connection
            command.upgrade(config, "d01010100101")

        async with writer.begin() as db:
            await db.run_sync(old_schema)
            await db.execute(
                text("CREATE TABLE retained_business (id integer PRIMARY KEY)")
            )
            await db.execute(text("INSERT INTO retained_business VALUES (42)"))
    args = options(tmp_path, role, monkeypatch, admin, bootstrap=bootstrap)
    result = await install.run(args)
    assert result["state"] == "prepared-admission-closed"
    await assert_closed(admin, writer, tmp_path)
    args.command = "resume"
    with pytest.raises(BusinessException):
        await install.run(args)
    await assert_closed(admin, writer, tmp_path)
    # Exercise actual permit expiry, rather than editing the protected timestamp.
    await asyncio.sleep(31)
    assert (await install.run(args))["processes_restarted"] is False
    async with writer.connect() as db:
        assert (
            await db.scalar(text("SELECT version_num FROM alembic_version"))
            == install.REVISION
        )
        assert await db.scalar(text("SELECT count(*) FROM sys_user")) >= 1
        assert (
            await db.scalar(
                text("SELECT mode FROM sys_user_field_policy_state WHERE tenant_id=0")
            )
            == "legacy"
        )
        owner = await db.scalar(
            text(
                "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='sys_user_field_policy_state'::regclass"
            )
        )
        assert owner == role
        if not bootstrap:
            assert await db.scalar(text("SELECT id FROM retained_business")) == 42
    with pytest.raises(BusinessException):
        await install.run(args)
    if not bootstrap:
        repeated = tmp_path / "repeat"
        repeated.mkdir()
        args = options(repeated, role, monkeypatch, admin, bootstrap=False)
        assert (await install.run(args))["state"] == "prepared-admission-closed"
        await assert_closed(admin, writer, repeated)


@pytest.mark.parametrize(
    "failure", ["migration", "seed", "occupied", "unknown-revision", "inflight"]
)
async def test_real_failures_keep_database_closed_without_success_receipt(
    deployment_database, tmp_path, monkeypatch, failure
):
    admin, writer, role, _ = deployment_database
    bootstrap = failure != "unknown-revision"
    if failure == "occupied":
        async with writer.begin() as db:
            await db.execute(text("CREATE TABLE retained (id integer)"))
    elif failure == "unknown-revision":
        async with writer.begin() as db:
            await db.execute(
                text("CREATE TABLE alembic_version (version_num varchar(32))")
            )
            await db.execute(text("INSERT INTO alembic_version VALUES ('unknown')"))
    elif failure == "migration":

        def broken(connection):
            connection.execute(text("CREATE TABLE should_rollback (id integer)"))
            raise RuntimeError("migration failure")

        monkeypatch.setattr(install, "_alembic", broken)
    elif failure == "seed":

        async def broken_seed(*_args, **_kwargs):
            raise RuntimeError("seed failure")

        monkeypatch.setattr(install.ops.AuthorizationRuntime, "seed", broken_seed)
    inflight = None
    if failure == "inflight":
        inflight = await writer.connect()
        await inflight.execute(text("SELECT 1"))
    try:
        args = options(tmp_path, role, monkeypatch, admin, bootstrap=bootstrap)
        with pytest.raises((BusinessException, RuntimeError)):
            await install.run(args)
        assert not (tmp_path / "install.json").exists()
        await assert_closed(admin, writer, tmp_path)
        args.command = "resume"
        with pytest.raises(BusinessException):
            await install.run(args)
    finally:
        if inflight is not None:
            await inflight.close()
