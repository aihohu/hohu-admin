from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jose import jwt
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.core.exceptions import AuthenticationException, AuthorizationException
from app.core.security import create_platform_access_token, get_password_hash
from app.modules.platform.auth import authenticate_platform_token
from app.modules.platform.constants import PLATFORM_AI_READ, PLATFORM_AI_WRITE
from app.modules.platform.schemas import PlatformLoginCredentials
from app.modules.platform.service import platform_auth_service


def test_platform_token_has_an_independent_claim_shape():
    token = create_platform_access_token(subject="91", principal_version=3)
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

    assert payload["type"] == "platform_access"
    assert payload["sub"] == "91"
    assert payload["pver"] == "3"
    assert "tid" not in payload


def test_platform_token_uses_an_independent_bounded_ttl(monkeypatch):
    monkeypatch.setattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", 600)
    monkeypatch.setattr(
        settings, "PLATFORM_ACCESS_TOKEN_EXPIRE_MINUTES", 17, raising=False
    )
    issued_at = datetime.now(UTC)

    token = create_platform_access_token(subject="91", principal_version=3)
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

    expires_at = datetime.fromtimestamp(payload["exp"], UTC)
    assert timedelta(minutes=16, seconds=55) <= expires_at - issued_at
    assert expires_at - issued_at <= timedelta(minutes=17, seconds=5)


async def test_tenant_access_token_cannot_become_a_platform_principal():
    payload = {
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "sub": "1",
        "tid": "0",
        "type": "access",
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    with pytest.raises(AuthorizationException) as exc_info:
        await authenticate_platform_token(token, AsyncMock())

    assert exc_info.value.error_code == "PLATFORM_ADMIN_REQUIRED"


async def test_platform_token_revalidates_status_version_and_permissions():
    principal = SimpleNamespace(
        principal_id=91,
        principal_name="platform-auditor",
        status="1",
        row_version=3,
        permissions=[PLATFORM_AI_READ, PLATFORM_AI_WRITE],
    )
    db = AsyncMock()
    db.scalar.return_value = principal
    token = create_platform_access_token(subject="91", principal_version=3)

    identity = await authenticate_platform_token(token, db)

    assert identity.principal_id == 91
    assert identity.permissions == frozenset({PLATFORM_AI_READ, PLATFORM_AI_WRITE})
    statement = db.scalar.await_args.args[0]
    assert "FOR SHARE" in str(statement.compile(dialect=postgresql.dialect())).upper()

    principal.row_version = 4
    with pytest.raises(AuthenticationException) as exc_info:
        await authenticate_platform_token(token, db)
    assert exc_info.value.error_code == "PLATFORM_TOKEN_INVALID"


async def test_platform_login_issues_no_tenant_or_refresh_authority():
    principal = SimpleNamespace(
        principal_id=91,
        principal_name="platform-operator",
        hashed_password=get_password_hash("a-long-test-password"),
        status="1",
        row_version=3,
        permissions=[PLATFORM_AI_READ, PLATFORM_AI_WRITE],
        last_login_at=None,
    )
    db = AsyncMock()
    db.scalar.return_value = principal

    token = await platform_auth_service.authenticate(
        db,
        PlatformLoginCredentials(
            principal_name="platform-operator",
            password="a-long-test-password",
        ),
    )
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

    assert payload["type"] == "platform_access"
    assert "tid" not in payload
    assert principal.last_login_at is not None
    db.flush.assert_awaited_once()
