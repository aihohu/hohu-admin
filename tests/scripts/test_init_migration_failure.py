"""A failed upgrade must never be disguised as a completed migration."""

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest


def test_initialization_stops_without_stamping_or_seeding(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts" / "init.py"
    spec = importlib.util.spec_from_file_location("project_init", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "init_env_file", lambda: None)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    monkeypatch.setattr(module.os.path, "exists", lambda _path: True)
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
    assert not any("stamp" in command for command in commands)
    assert not any(
        any("init_db.py" in part for part in command) for command in commands
    )
