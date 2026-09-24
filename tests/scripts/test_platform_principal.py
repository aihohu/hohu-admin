from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import BusinessException
from app.core.security import get_password_hash
from app.modules.platform.constants import (
    PLATFORM_AI_READ,
    PLATFORM_TENANT_READ,
)
from app.modules.platform.models import PlatformPrincipal
from tools.ops import platform_principal as cli

pytest_plugins = ("tests.modules.platform.conftest",)


# ============ create（首个平台主体 bootstrap） ============


def test_create_standalone_loads_platform_foreign_key_metadata():
    repository = Path(__file__).resolve().parents[2]
    code = (
        "from tools.ops import platform_principal; "
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


def test_create_requires_explicit_permissions(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "platform_principal.py",
            "create",
            "--principal-name",
            "platform_admin",
            "--display-name",
            "Platform Administrator",
        ],
    )

    with pytest.raises(SystemExit):
        cli._arguments()


def test_create_password_requires_letters_and_digits(monkeypatch):
    values = iter(["abcdefghijkl", "abcdefghijkl"])
    monkeypatch.setattr(cli, "getpass", lambda _prompt: next(values))

    with pytest.raises(ValueError, match="letter and digit"):
        cli._read_password()


async def test_create_refuses_any_additional_platform_principal(
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

    monkeypatch.setattr(cli, "AsyncSessionLocal", SessionProxy)
    arguments = SimpleNamespace(
        principal_name="second_platform_admin",
        display_name="Second Platform Administrator",
        permissions=[PLATFORM_AI_READ],
    )

    with pytest.raises(ValueError, match="already exists"):
        await cli._bootstrap(arguments, "another-password1")


async def test_create_serializes_first_principal_creation():
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

    await cli._create_first_principal(session, arguments, "first-password1")

    session.execute.assert_awaited_once()


async def test_create_rejects_control_characters_before_database_access():
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
        await cli._create_first_principal(session, arguments, "first-password1")

    session.execute.assert_not_awaited()


# ============ replace-permissions（权限替换） ============


def _replace_arguments(**overrides):
    values = {
        "principal_name": "platform_admin",
        "permissions": [PLATFORM_TENANT_READ],
        "reason": "Grant reviewed Plan 5-B support access",
        "ticket_id": "OPS-5001",
        "correlation_id": "ops-5001-permission-replace",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_permission_replacement_requires_explicit_audit_context(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "platform_principal.py",
            "replace-permissions",
            "--principal-name",
            "platform_admin",
            "--permission",
            PLATFORM_TENANT_READ,
        ],
    )

    with pytest.raises(SystemExit):
        cli._arguments()


async def test_permission_replacement_is_exact_and_revokes_old_version(db_session):
    principal = PlatformPrincipal(
        principal_name="platform_admin",
        display_name="Platform Administrator",
        hashed_password=get_password_hash("existing-password1"),
        permissions=[PLATFORM_AI_READ],
    )
    db_session.add(principal)
    await db_session.flush()
    original_version = principal.row_version

    changed = await cli._apply_permission_replacement(
        db_session,
        principal_id=principal.principal_id,
        expected_row_version=original_version,
        current_password="existing-password1",
        permissions=(PLATFORM_TENANT_READ,),
    )

    assert changed is True
    assert principal.permissions == [PLATFORM_TENANT_READ]
    assert principal.row_version == original_version + 1


async def test_permission_replacement_fails_closed_on_security_version_race(db_session):
    principal = PlatformPrincipal(
        principal_name="race_platform_admin",
        display_name="Race Platform Administrator",
        hashed_password=get_password_hash("existing-password1"),
        permissions=[PLATFORM_AI_READ],
    )
    db_session.add(principal)
    await db_session.flush()

    with pytest.raises(BusinessException) as exc_info:
        await cli._apply_permission_replacement(
            db_session,
            principal_id=principal.principal_id,
            expected_row_version=principal.row_version + 1,
            current_password="existing-password1",
            permissions=(PLATFORM_TENANT_READ,),
        )

    assert exc_info.value.error_code == "PLATFORM_PRINCIPAL_CHANGED"
    assert principal.permissions == [PLATFORM_AI_READ]


async def test_offline_authorization_rejects_secret_context_before_change():
    persist = AsyncMock(return_value=1)
    snapshot = cli.PrincipalSnapshot(
        principal_id=10,
        principal_name="platform_admin",
        row_version=1,
    )

    with pytest.raises(BusinessException) as exc_info:
        await cli._authorize_permission_replacement(
            snapshot,
            _replace_arguments(reason="token=abcdefghijklmnop123456"),
            persist=persist,
        )

    assert exc_info.value.error_code == "PLATFORM_AUDIT_CONTEXT_SENSITIVE"
    assert persist.await_args.kwargs["event_type"] == "denied"


def test_main_never_prints_unexpected_exception_details(monkeypatch, capsys):
    secret = "password=never-render-this"
    monkeypatch.setattr(
        cli,
        "_arguments",
        lambda: _replace_arguments(command="replace-permissions"),
    )
    monkeypatch.setattr(cli, "_read_current_password", lambda: secret)

    async def fail_safely(_arguments, _password):
        raise RuntimeError(secret)

    monkeypatch.setattr(cli, "_replace", fail_safely)

    with pytest.raises(SystemExit):
        cli.main()

    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "PLATFORM_PERMISSION_REPLACE_FAILED" in captured.err
