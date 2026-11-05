"""CI initialization exercises the production bootstrap and explicit recovery."""

import json
from unittest.mock import AsyncMock

import pytest

from tests.release import bootstrap_database as module


async def test_test_bootstrap_keeps_failure_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("HOHU_TEST_DATABASE_EPHEMERAL", "true")
    monkeypatch.setenv("AUTHORIZATION_MAINTENANCE_PASSWORD", "Test-only-123!")
    run = AsyncMock(side_effect=RuntimeError("injected seed failure"))
    monkeypatch.setattr(module.ops, "run_command", run)
    with pytest.raises(RuntimeError, match="injected"):
        await module.bootstrap(tmp_path, "a" * 40)
    assert run.await_count == 1
    assert run.await_args.args[0].command == "bootstrap"


async def test_test_bootstrap_uses_production_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("HOHU_TEST_DATABASE_EPHEMERAL", "true")
    monkeypatch.setenv("AUTHORIZATION_MAINTENANCE_PASSWORD", "Test-only-123!")
    run = AsyncMock(return_value={"state": "initialized-admission-closed"})
    sleep = AsyncMock()
    monkeypatch.setattr(module.ops, "run_command", run)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    await module.bootstrap(tmp_path, "a" * 40)
    commands = [call.args[0] for call in run.await_args_list]
    assert [args.command for args in commands] == ["bootstrap", "bootstrap-resume"]
    assert commands[0].inventory == commands[1].inventory
    inventory = json.loads(commands[0].inventory.read_text())
    assert inventory["bootstrap"] is True
    assert inventory["manifest"]["build_sha"] == "a" * 40
    sleep.assert_awaited_once_with(31)


async def test_test_bootstrap_requires_explicit_ephemeral_target(tmp_path, monkeypatch):
    monkeypatch.delenv("HOHU_TEST_DATABASE_EPHEMERAL", raising=False)
    with pytest.raises(ValueError):
        await module.bootstrap(tmp_path, "a" * 40)
    assert not list(tmp_path.iterdir())
