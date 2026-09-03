from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import BusinessException
from app.core.security import get_password_hash
from app.modules.platform.constants import PLATFORM_AI_READ, PLATFORM_TENANT_READ
from app.modules.platform.models import PlatformPrincipal
from scripts import replace_platform_principal_permissions as replacement

pytest_plugins = ("tests.modules.platform.conftest",)


def _arguments(**overrides):
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
            "replace_platform_principal_permissions.py",
            "--principal-name",
            "platform_admin",
            "--permission",
            PLATFORM_TENANT_READ,
        ],
    )

    with pytest.raises(SystemExit):
        replacement._arguments()


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

    changed = await replacement._apply_permission_replacement(
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
        await replacement._apply_permission_replacement(
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
    snapshot = replacement.PrincipalSnapshot(
        principal_id=10,
        principal_name="platform_admin",
        row_version=1,
    )

    with pytest.raises(BusinessException) as exc_info:
        await replacement._authorize_permission_replacement(
            snapshot,
            _arguments(reason="token=abcdefghijklmnop123456"),
            persist=persist,
        )

    assert exc_info.value.error_code == "PLATFORM_AUDIT_CONTEXT_SENSITIVE"
    assert persist.await_args.kwargs["event_type"] == "denied"


def test_main_never_prints_unexpected_exception_details(monkeypatch, capsys):
    secret = "password=never-render-this"
    monkeypatch.setattr(replacement, "_arguments", lambda: _arguments())
    monkeypatch.setattr(replacement, "_read_current_password", lambda: secret)

    async def fail_safely(_arguments, _password):
        raise RuntimeError(secret)

    monkeypatch.setattr(replacement, "_replace", fail_safely)

    with pytest.raises(SystemExit):
        replacement.main()

    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "PLATFORM_PERMISSION_REPLACE_FAILED" in captured.err
