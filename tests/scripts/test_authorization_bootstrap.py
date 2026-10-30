"""Fresh installation is a database proof, never an empty process inventory."""

import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.core.authorization_bootstrap import require_bootstrap
from app.core.authorization_maintenance import require_maintenance
from app.core.exceptions import BusinessException
from app.db.authorization_runtime import AuthorizationRuntime
from tests.scripts.test_authorization_maintenance_database import (
    deployment_database as deployment_database,
)
from tools.ops import authorization_maintenance as ops


@pytest.fixture
async def fresh_database(deployment_database):
    admin, writer, role, name = deployment_database

    def migrate(connection):
        root = Path(__file__).resolve().parents[2] / "alembic/versions"
        with Operations.context(MigrationContext.configure(connection)):
            for filename in (
                "bf244f9a8b76_baseline.py",
                "d01010100101_squash_to_head.py",
                "e0202a000001_user_field_policy_storage.py",
            ):
                runpy.run_path(str(root / filename))["upgrade"]()

    async with admin.begin() as db:
        await db.run_sync(migrate)
        await db.execute(text("CREATE TABLE alembic_version (version_num varchar(32))"))
        await db.execute(text("INSERT INTO alembic_version VALUES ('e0202a000001')"))
    return admin, writer, role, name


async def test_fresh_bootstrap_has_separate_proof_and_cannot_replay(
    fresh_database, tmp_path
):
    admin, writer, role, _ = fresh_database
    manifest = ops.StopManifest("fresh", "a" * 40, "direct", ("api", "scheduler"))
    async with ops.PostgresStop(admin, tmp_path, manifest, (role,)) as guard:
        await guard.prepare_bootstrap()
        permit = await guard.bootstrap_fence()
        assert (
            require_bootstrap(permit, "seed.system").database_oid == guard.database_oid
        )
        with pytest.raises(BusinessException):
            require_maintenance(permit, "seed.system")
        with pytest.raises(BusinessException):
            require_bootstrap(permit, "seed.agents")
        with pytest.raises(Exception) as refused:
            async with writer.connect():
                pytest.fail("bootstrap admitted a new writer")
        assert getattr(refused.value, "sqlstate", None) == "55000"
        result = await AuthorizationRuntime(guard.runtime_engine).seed(
            action="seed.system", fence=permit, admin_password="Fresh-Example-123!"
        )
        assert result["fresh"] is True
        with pytest.raises(BusinessException):
            await guard.bootstrap_fence()
        # A permit is single use, including after an unknown COMMIT acknowledgement.
        with pytest.raises(BusinessException):
            await AuthorizationRuntime(guard.runtime_engine).seed(
                action="seed.system", fence=permit, admin_password="Fresh-Example-123!"
            )
        await guard.require_quiescent()
        with pytest.raises(BusinessException):
            await guard.resume_bootstrap()
    record = ops.read_object(tmp_path / "stop.json")
    record["observed_at"] = (datetime.now(UTC) - timedelta(seconds=31)).isoformat()
    (tmp_path / "stop.json").write_text(json.dumps(record))
    async with ops.PostgresStop(admin, tmp_path, manifest, (role,)) as recovery:
        await recovery.resume_bootstrap()
    assert not (tmp_path / "stop.json").exists()
    async with writer.connect() as db:
        assert await db.scalar(text("SELECT 1")) == 1


@pytest.mark.parametrize("dirty", ["history", "epoch", "registered", "inflight"])
async def test_bootstrap_refuses_existing_deployment(fresh_database, tmp_path, dirty):
    admin, writer, role, _ = fresh_database
    manifest = ops.StopManifest("fresh", "a" * 40, "direct", ("api", "scheduler"))
    inflight = None
    if dirty == "history":
        async with admin.begin() as db:
            await db.execute(text("CREATE TABLE retained_history (id integer)"))
            await db.execute(text("INSERT INTO retained_history VALUES (1)"))
    if dirty == "epoch":
        async with admin.begin() as db:
            await db.execute(
                text("UPDATE sys_user_field_policy_global_guard SET epoch=2")
            )
    if dirty == "registered":
        (tmp_path / "process-123.json").write_text(json.dumps({"state": "exited"}))
    if dirty == "inflight":
        inflight = await writer.connect()
        await inflight.execute(text("SELECT 1"))
    try:
        async with ops.PostgresStop(admin, tmp_path, manifest, (role,)) as guard:
            with pytest.raises(BusinessException):
                await guard.prepare_bootstrap()
                await guard.bootstrap_fence()
    finally:
        if inflight is not None:
            await inflight.close()


def test_bootstrap_inventory_is_not_a_stop_inventory(tmp_path):
    inventory = tmp_path / "bootstrap.json"
    inventory.write_text(
        json.dumps(
            {
                "manifest": {
                    "deployment_id": "fresh",
                    "build_sha": "a" * 40,
                    "mode": "direct",
                    "surfaces": ["api", "scheduler"],
                },
                "application_roles": ["application"],
                "bootstrap": True,
            }
        )
    )
    _, processes, roles = ops.read_inventory(inventory, bootstrap=True)
    assert processes is None and roles == ("application",)
    with pytest.raises(BusinessException):
        ops.read_inventory(inventory)
    args = ops.seed_arguments(
        "seed.system", ["--inventory", str(inventory), "--bootstrap"]
    )
    assert args.command == "bootstrap" and args.action == "seed.system"
    for action in ("seed.agents", "seed.settings", "seed.menus"):
        with pytest.raises(SystemExit):
            ops.seed_arguments(action, ["--inventory", str(inventory), "--bootstrap"])
    with pytest.raises(SystemExit):
        ops.arguments(
            ["bootstrap", "--inventory", str(inventory), "--action", "seed.agents"]
        )


async def test_bootstrap_command_uses_fixed_owner_and_leaves_admission_closed(
    fresh_database, tmp_path, monkeypatch
):
    admin, _, role, _ = fresh_database
    manifest = ops.StopManifest("fresh", "a" * 40, "compose", ("api", "scheduler"))
    inventory = tmp_path / "fresh.json"
    inventory.write_text(
        json.dumps(
            {
                "manifest": ops.asdict(manifest),
                "application_roles": [role],
                "bootstrap": True,
            }
        )
    )
    monkeypatch.setenv(
        "AUTHORIZATION_MAINTENANCE_DATABASE_URL",
        admin.url.render_as_string(hide_password=False),
    )
    monkeypatch.setenv("AUTHORIZATION_MAINTENANCE_PASSWORD", "Fresh-Example-123!")
    monkeypatch.setattr(ops.settings, "RELEASE_BUILD_SHA", manifest.build_sha)
    args = ops.arguments(
        ["bootstrap", "--inventory", str(inventory), "--control-dir", str(tmp_path)]
    )
    result = await ops.run_command(args)
    assert result["state"] == "initialized-admission-closed"
    assert result["result"]["fresh"] is True
    assert (tmp_path / "stop.json").exists()


async def test_ci_bootstrap_recovers_only_after_real_expiry(
    fresh_database, tmp_path, monkeypatch
):
    from tests.release.bootstrap_database import bootstrap  # noqa: PLC0415

    admin, writer, _, _ = fresh_database
    url = admin.url.render_as_string(hide_password=False)
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("HOHU_TEST_DATABASE_EPHEMERAL", "true")
    monkeypatch.setenv("AUTHORIZATION_MAINTENANCE_DATABASE_URL", url)
    monkeypatch.setenv("AUTHORIZATION_MAINTENANCE_PASSWORD", "Fresh-Example-123!")
    monkeypatch.setattr(ops.settings, "DATABASE_URL", url)
    monkeypatch.setattr(ops.settings, "RELEASE_BUILD_SHA", "a" * 40)
    await bootstrap(tmp_path, "a" * 40)
    assert not (tmp_path / "stop.json").exists()
    async with writer.connect() as db:
        assert await db.scalar(text("SELECT 1")) == 1
