from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.security import get_password_hash
from app.modules.platform.constants import PLATFORM_AI_READ
from app.modules.platform.models import PlatformPrincipal
from scripts import bootstrap_platform_principal as bootstrap

pytest_plugins = ("tests.modules.platform.conftest",)


def test_bootstrap_standalone_loads_platform_foreign_key_metadata():
    repository = Path(__file__).resolve().parents[2]
    code = (
        "from scripts import bootstrap_platform_principal; "
        "from app.db.base import Base; "
        "assert 'sys_tenant' in Base.metadata.tables"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_bootstrap_requires_explicit_permissions(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap_platform_principal.py",
            "--principal-name",
            "platform_admin",
            "--display-name",
            "Platform Administrator",
        ],
    )

    with pytest.raises(SystemExit):
        bootstrap._arguments()


def test_bootstrap_password_requires_letters_and_digits(monkeypatch):
    values = iter(["abcdefghijkl", "abcdefghijkl"])
    monkeypatch.setattr(bootstrap, "getpass", lambda _prompt: next(values))

    with pytest.raises(ValueError, match="letter and digit"):
        bootstrap._read_password()


async def test_bootstrap_refuses_any_additional_platform_principal(
    db_session, monkeypatch
):
    existing = PlatformPrincipal(
        principal_name="existing_platform_admin",
        display_name="Existing Platform Administrator",
        hashed_password=get_password_hash("existing-password1"),
        permissions=[PLATFORM_AI_READ],
    )
    db_session.add(existing)
    await db_session.flush()

    class SessionProxy:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, statement):
            return await db_session.scalar(statement)

        async def execute(self, statement, parameters=None):
            return await db_session.execute(statement, parameters)

        def add(self, value):
            db_session.add(value)

        async def commit(self):
            await db_session.flush()

    monkeypatch.setattr(bootstrap, "AsyncSessionLocal", SessionProxy)
    arguments = SimpleNamespace(
        principal_name="second_platform_admin",
        display_name="Second Platform Administrator",
        permissions=[PLATFORM_AI_READ],
    )

    with pytest.raises(ValueError, match="already exists"):
        await bootstrap._bootstrap(arguments, "another-password1")


async def test_bootstrap_serializes_first_principal_creation():
    session = SimpleNamespace(
        execute=AsyncMock(),
        scalar=AsyncMock(return_value=None),
        add=MagicMock(),
        flush=AsyncMock(),
    )
    arguments = SimpleNamespace(
        principal_name="first_platform_admin",
        display_name="First Platform Administrator",
        permissions=[PLATFORM_AI_READ],
    )

    await bootstrap._create_first_principal(session, arguments, "first-password1")

    session.execute.assert_awaited_once()


async def test_bootstrap_rejects_control_characters_before_database_access():
    session = SimpleNamespace(
        execute=AsyncMock(),
        scalar=AsyncMock(return_value=None),
        add=MagicMock(),
        flush=AsyncMock(),
    )
    arguments = SimpleNamespace(
        principal_name="first_platform_admin",
        display_name="Platform\nAdministrator",
        permissions=[PLATFORM_AI_READ],
    )

    with pytest.raises(ValueError, match="display name"):
        await bootstrap._create_first_principal(session, arguments, "first-password1")

    session.execute.assert_not_awaited()
