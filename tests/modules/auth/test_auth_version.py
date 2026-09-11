"""Per-user authentication version and JWT boundary regressions."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import JWTError, jwt

from app.core.config import settings
from app.core.exceptions import AuthenticationException
from app.core.security import (
    TENANT_ACCESS_AUDIENCE,
    TENANT_REFRESH_AUDIENCE,
    TOKEN_ISSUER,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
)
from app.modules.auth.service import get_current_user
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User


def _token_user(*, auth_version: int) -> User:
    tenant = Tenant(
        tenant_id=0,
        tenant_code="default",
        tenant_name="Default Tenant",
        status="1",
        lifecycle_state="active",
        row_version=1,
    )
    user = User(
        user_id=123,
        tenant_id=0,
        user_name="alice",
        hashed_password="unused",
        status="1",
        auth_version=auth_version,
    )
    user.tenant = tenant
    return user


def _scalar_result(value):
    result = MagicMock()
    result.scalars.return_value.first.return_value = value
    return result


def test_tenant_tokens_have_distinct_verified_boundaries() -> None:
    access = create_access_token(
        subject="123", tenant_id=0, tenant_version=1, user_version=7
    )
    refresh = create_refresh_token(
        subject="123", tenant_id=0, tenant_version=1, user_version=7
    )

    access_payload = decode_access_token(access)
    refresh_payload = decode_refresh_token(refresh)

    assert access_payload["iss"] == TOKEN_ISSUER
    assert access_payload["aud"] == TENANT_ACCESS_AUDIENCE
    assert refresh_payload["aud"] == TENANT_REFRESH_AUDIENCE
    assert access_payload["uver"] == "7"
    assert refresh_payload["uver"] == "7"
    with pytest.raises(JWTError):
        decode_refresh_token(access)
    with pytest.raises(JWTError):
        decode_access_token(refresh)


def test_legacy_token_without_issuer_or_audience_is_rejected() -> None:
    legacy = jwt.encode(
        {
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "sub": "123",
            "tid": "0",
            "tver": "1",
            "uver": "1",
            "type": "access",
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )

    with pytest.raises(JWTError):
        decode_access_token(legacy)


async def test_access_token_rejects_stale_user_auth_version() -> None:
    user = _token_user(auth_version=2)
    token = create_access_token(
        subject="123", tenant_id=0, tenant_version=1, user_version=1
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar_result(user))

    with patch(
        "app.modules.auth.service._is_blacklisted", AsyncMock(return_value=False)
    ):
        with pytest.raises(AuthenticationException) as exc_info:
            await get_current_user(token=token, db=db)

    assert exc_info.value.error_code == "TOKEN_EXPIRED"
