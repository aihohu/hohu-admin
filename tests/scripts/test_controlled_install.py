"""The installer never turns a migration or seed failure into application startup."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import BusinessException
from tools.ops import controlled_install as install


@pytest.mark.parametrize("revision", ["e020f2a00001", "e020f2b00001"])
async def test_upgrade_accepts_each_immediately_preceding_field_slice(
    monkeypatch, revision
):
    state = {"revision": revision, "migrated": False}

    class Connection:
        async def execute(self, *_args, **_kwargs):
            return None

        async def scalar(self, *_args, **_kwargs):
            return False  # the dedicated application owner is not a superuser

        async def run_sync(self, function):
            return function(self)

    @asynccontextmanager
    async def begin():
        yield Connection()

    def migrate(_connection):
        state.update(revision=install.REVISION, migrated=True)

    monkeypatch.setattr(
        install.MigrationContext,
        "configure",
        lambda _connection: SimpleNamespace(
            get_current_heads=lambda: (state["revision"],)
        ),
    )
    monkeypatch.setattr(install, "_alembic", migrate)
    guard = SimpleNamespace(
        roles=("application",),
        engine=SimpleNamespace(url=SimpleNamespace(username="operator")),
        runtime_engine=SimpleNamespace(begin=begin),
    )
    await install.migrate(guard, False)
    assert state == {"revision": install.REVISION, "migrated": True}


@pytest.fixture
def installation(tmp_path, monkeypatch):
    events = []

    async def event(name):
        events.append(name)

    guard = SimpleNamespace(
        directory=tmp_path,
        database_oid=123,
        manifest=SimpleNamespace(build_sha="a" * 40, digest="b" * 64),
        runtime_engine=object(),
        prepare_bootstrap=lambda: event("fence"),
        prepare=lambda _processes: event("fence"),
        require_quiescent=lambda: event("observe"),
        bootstrap_fence=lambda: event("permit"),
    )
    (tmp_path / "stop.json").write_text('{"fenced":true}', encoding="utf-8")
    processes = SimpleNamespace(stop=lambda: event("stop"))
    guard.observe = lambda _: event("observe")

    async def migrate(*_args, **_kwargs):
        await event("migrate")

    async def seed(**_kwargs):
        await event("seed")
        return {"fresh": True}

    monkeypatch.setattr(install, "migrate", migrate)
    monkeypatch.setattr(
        install.ops, "AuthorizationRuntime", lambda _: SimpleNamespace(seed=seed)
    )

    async def execute(*_args):
        return await seed()

    monkeypatch.setattr(install.ops, "execute_action", AsyncMock(side_effect=execute))
    monkeypatch.setenv("AUTHORIZATION_MAINTENANCE_PASSWORD", "unit-seed-password")
    return guard, processes, events


@pytest.mark.parametrize("bootstrap", [True, False])
async def test_prepare_keeps_admission_closed_and_records_success(
    installation, bootstrap
):
    guard, processes, events = installation
    result = await install.prepare(guard, None if bootstrap else processes, bootstrap)
    assert result["state"] == "prepared-admission-closed"
    assert events.index("fence") < events.index("migrate") < events.index("seed")
    if not bootstrap:
        assert events.index("stop") < events.index("migrate")
    assert (guard.directory / "stop.json").exists()
    assert install.require_receipt(guard, bootstrap)["revision"] == "e020f3a00001"


@pytest.mark.parametrize("bootstrap", [True, False])
@pytest.mark.parametrize("failure", ["migration", "seed", "cancel", "receipt"])
async def test_failed_prepare_never_produces_success_or_resumes(
    installation, monkeypatch, failure, bootstrap
):
    guard, processes, events = installation

    async def fail(*_args, **_kwargs):
        raise asyncio.CancelledError if failure == "cancel" else RuntimeError("failure")

    if failure in {"migration", "cancel"}:
        monkeypatch.setattr(install, "migrate", fail)
    elif failure == "seed":
        monkeypatch.setattr(
            install.ops, "AuthorizationRuntime", lambda _: SimpleNamespace(seed=fail)
        )
        monkeypatch.setattr(install.ops, "execute_action", fail)
    else:
        monkeypatch.setattr(
            install,
            "write_receipt",
            lambda *_: (_ for _ in ()).throw(OSError("disk full")),
        )
    with pytest.raises((RuntimeError, asyncio.CancelledError, OSError)):
        await install.prepare(guard, None if bootstrap else processes, bootstrap)
    assert (guard.directory / "stop.json").exists()
    assert not (guard.directory / "install.json").exists()
    if failure in {"migration", "cancel"}:
        assert "seed" not in events


async def test_stop_failure_prevents_migration(installation):
    guard, processes, events = installation

    async def fail():
        raise RuntimeError("stop rejected")

    processes.stop = fail
    with pytest.raises(RuntimeError):
        await install.prepare(guard, processes, False)
    assert "migrate" not in events and "seed" not in events
    assert not (guard.directory / "install.json").exists()


async def test_policy_upgrade_identity_preflight_precedes_any_migration(
    installation, monkeypatch
):
    guard, processes, events = installation

    async def missing(_):
        events.append("identity")
        raise BusinessException(
            409, "missing", error_code="FIELD_POLICY_ADMIN_PREFLIGHT_REQUIRED"
        )

    monkeypatch.setattr(
        "tools.ops.policy_preparation_identity.require_preparation_administrators",
        missing,
    )
    with pytest.raises(BusinessException):
        await install.prepare(guard, processes, False, policy_port=8765)
    assert "identity" in events
    assert "migrate" not in events
    assert not (guard.directory / "install.json").exists()


@pytest.mark.parametrize("change", ["stop", "build", "bootstrap", "extra", "missing"])
async def test_receipt_cannot_authorize_another_or_failed_operation(
    installation, change
):
    guard, _, _ = installation
    await install.prepare(guard, None, True)
    path = guard.directory / "install.json"
    if change == "stop":
        (guard.directory / "stop.json").write_text("{}", encoding="utf-8")
    elif change == "missing":
        path.unlink()
    else:
        record = json.loads(path.read_text(encoding="utf-8"))
        record[
            {"build": "build", "bootstrap": "bootstrap", "extra": "unknown"}[change]
        ] = "changed"
        path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(BusinessException):
        install.require_receipt(guard, True)


@pytest.mark.parametrize(
    "arguments", [[], ["prepare"], ["start", "--inventory", "i", "--control-dir", "c"]]
)
def test_cli_requires_explicit_bounded_protocol(arguments):
    with pytest.raises(SystemExit):
        install.arguments(arguments)


async def test_fsync_failure_does_not_publish_a_usable_receipt(
    installation, monkeypatch
):
    guard, _, _ = installation

    def fail(_fd):
        raise OSError("disk failure")

    monkeypatch.setattr(install.os, "fsync", fail)
    with pytest.raises(OSError):
        await install.prepare(guard, None, True)
    assert not (guard.directory / "install.json").exists()
    assert (guard.directory / "stop.json").exists()


def test_cli_error_does_not_disclose_database_or_driver_details(monkeypatch, capsys):
    monkeypatch.setattr(install, "arguments", lambda: object())
    monkeypatch.setattr(
        install,
        "run",
        AsyncMock(side_effect=RuntimeError("postgres://secret-password")),
    )
    with pytest.raises(SystemExit) as error:
        install.main()
    assert "secret-password" not in str(error.value)
    assert "secret-password" not in capsys.readouterr().out


async def test_invalid_seed_password_is_rejected_before_database_fencing(
    monkeypatch, tmp_path
):
    manifest = SimpleNamespace(build_sha="a" * 40)
    monkeypatch.setattr(
        install.ops, "read_inventory", lambda *_a, **_k: (manifest, None, ["app_role"])
    )
    monkeypatch.setattr(install.ops.settings, "RELEASE_BUILD_SHA", manifest.build_sha)
    monkeypatch.setenv(
        "AUTHORIZATION_MAINTENANCE_DATABASE_URL", "postgresql+asyncpg://unused/unused"
    )
    monkeypatch.setenv("AUTHORIZATION_MAINTENANCE_PASSWORD", "Invalid-" + "x" * 30)

    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid input reached the database")

    monkeypatch.setattr(install, "create_async_engine", forbidden)
    args = install.arguments(
        [
            "prepare",
            "--bootstrap",
            "--inventory",
            "unused",
            "--control-dir",
            str(tmp_path),
        ]
    )
    with pytest.raises(ValueError):
        await install.run(args)
