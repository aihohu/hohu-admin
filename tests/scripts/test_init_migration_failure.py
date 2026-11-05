"""A failed upgrade must never be disguised as a completed migration."""

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest


def test_initialization_stops_without_stamping_or_seeding(monkeypatch):
    monkeypatch.setenv("AUTHORIZATION_MAINTENANCE_INVENTORY", "fresh.json")
    path = Path(__file__).resolve().parents[2] / "scripts" / "init.py"
    spec = importlib.util.spec_from_file_location("project_init", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "init_env_file", lambda: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    monkeypatch.setattr(
        module, "dotenv_values", lambda _path: {"HOHU_ADMIN_PASSWORD": "TestSeed123"}
    )
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if "alembic" in command and "upgrade" in command:
            raise subprocess.CalledProcessError(1, command)
        return Mock(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        module.init_project()
    assert exc.value.code == 1
    assert len(commands) == 1 and "alembic" in commands[0]
    assert not any("stamp" in command for command in commands)
    assert not any(
        any("scripts.init_db" in part for part in command) for command in commands
    )


def test_initialization_forwards_explicit_maintenance_inventory(monkeypatch):
    from scripts import init as module  # noqa: PLC0415

    monkeypatch.setattr(module, "init_env_file", lambda: None)
    monkeypatch.setattr(
        module, "dotenv_values", lambda _path: {"HOHU_ADMIN_PASSWORD": "TestSeed123"}
    )
    monkeypatch.setenv("AUTHORIZATION_MAINTENANCE_INVENTORY", "fresh.json")
    monkeypatch.setenv("AUTHORIZATION_CONTROL_DIR", "explicit-control")
    monkeypatch.setenv("AUTHORIZATION_BOOTSTRAP", "true")
    monkeypatch.delenv("HOHU_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("AUTHORIZATION_MAINTENANCE_PASSWORD", raising=False)
    calls = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )
    module.init_project()
    seed, options = calls[-1]
    assert seed[2:] == [
        "scripts.init_db",
        "--inventory",
        "fresh.json",
        "--control-dir",
        "explicit-control",
        "--bootstrap",
    ]
    assert options["env"]["AUTHORIZATION_MAINTENANCE_PASSWORD"] == "TestSeed123"


def test_initialization_requires_inventory_before_migration(monkeypatch):
    from scripts import init as module  # noqa: PLC0415

    monkeypatch.setattr(module, "init_env_file", lambda: None)
    monkeypatch.setattr(
        module, "dotenv_values", lambda _path: {"HOHU_ADMIN_PASSWORD": "TestSeed123"}
    )
    monkeypatch.delenv("AUTHORIZATION_MAINTENANCE_INVENTORY", raising=False)
    run = Mock()
    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(SystemExit):
        module.init_project()
    run.assert_not_called()
