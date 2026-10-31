"""Fixed audit attribution and token parsing; no cached authority fallback."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt

from app.core.config import settings
from app.core.security import (
    TENANT_ACCESS_AUDIENCE,
    TOKEN_ISSUER,
    create_access_token,
    create_refresh_token,
)
from app.middleware.audit_middleware import (
    AuditLogMiddleware,
    _parse_identity_from_token,
    _resolve_username,
)


@pytest.fixture
def fake_request_factory():
    def _make(token: str | None):
        request = MagicMock()
        request.headers = {}
        if token is not None:
            request.headers["Authorization"] = f"Bearer {token}"
        return request

    return _make


@pytest.mark.parametrize("value", ["alice", None])
async def test_resolve_username_uses_fixed_current_read(value):
    runtime = MagicMock()
    runtime.http_audit.username = AsyncMock(return_value=value)
    with patch(
        "app.middleware.audit_middleware.get_authorization_runtime",
        return_value=runtime,
    ):
        assert await _resolve_username(0, 12345) == value
    runtime.http_audit.username.assert_awaited_once_with(tenant_id=0, user_id=12345)


async def test_resolve_username_failure_has_no_fallback():
    runtime = MagicMock()
    runtime.http_audit.username = AsyncMock(side_effect=TimeoutError)
    with patch(
        "app.middleware.audit_middleware.get_authorization_runtime",
        return_value=runtime,
    ):
        assert await _resolve_username(0, 12345) is None
    assert runtime.http_audit.username.await_count == 1


def _make_token(
    *, sub: str, tid: str = "0", token_type: str = "access", expired: bool = False
):
    if not expired and token_type == "access":
        return create_access_token(
            subject=sub, tenant_id=int(tid), tenant_version=1, user_version=1
        )
    if not expired and token_type == "refresh":
        return create_refresh_token(
            subject=sub, tenant_id=int(tid), tenant_version=1, user_version=1
        )
    exp = datetime.now(UTC) + (
        timedelta(seconds=-10) if expired else timedelta(minutes=5)
    )
    payload = {
        "exp": exp,
        "iss": TOKEN_ISSUER,
        "aud": TENANT_ACCESS_AUDIENCE,
        "sub": sub,
        "tid": tid,
        "tver": "1",
        "uver": "1",
        "type": token_type,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def test_parse_user_id_rejects_refresh_token(fake_request_factory):
    refresh = _make_token(sub="123", token_type="refresh")
    assert _parse_identity_from_token(fake_request_factory(refresh)) is None


def test_parse_user_id_rejects_download_token(fake_request_factory):
    download = _make_token(sub="123", token_type="ai_result_download")
    assert _parse_identity_from_token(fake_request_factory(download)) is None


def test_parse_identity_returns_tenant_and_user_for_access_token(fake_request_factory):
    access = _make_token(sub="456", token_type="access")
    assert _parse_identity_from_token(fake_request_factory(access)) == (0, 456)


def test_parse_user_id_rejects_expired_token(fake_request_factory):
    expired = _make_token(sub="789", token_type="access", expired=True)
    assert _parse_identity_from_token(fake_request_factory(expired)) is None


def test_parse_user_id_missing_auth_header(fake_request_factory):
    assert _parse_identity_from_token(fake_request_factory(None)) is None


@pytest.mark.parametrize(
    "content_type",
    ["multipart/form-data; boundary=test", "application/octet-stream"],
)
async def test_non_json_request_body_is_never_read_for_audit(content_type: str) -> None:
    request = MagicMock()
    request.method = "POST"
    request.url.path = "/system/file/upload"
    request.headers = {"content-type": content_type}
    request.body = AsyncMock(side_effect=AssertionError("body must stay streaming"))
    response = MagicMock(status_code=200)
    call_next = AsyncMock(return_value=response)
    runtime = MagicMock()
    runtime.http_audit.operation = AsyncMock()

    with (
        patch(
            "app.middleware.audit_middleware._get_user_info",
            AsyncMock(return_value=(0, 123, "alice")),
        ),
        patch(
            "app.middleware.audit_middleware.get_authorization_runtime",
            return_value=runtime,
        ),
        patch(
            "app.middleware.audit_middleware.get_client_ip", return_value="127.0.0.1"
        ),
    ):
        result = await AuditLogMiddleware(AsyncMock()).dispatch(request, call_next)

    assert result is response
    request.body.assert_not_awaited()
    runtime.http_audit.operation.assert_awaited_once()
    assert runtime.http_audit.operation.await_args.kwargs["request_params"] is None


async def test_audit_failure_preserves_primary_response_without_retry():
    request = MagicMock()
    request.method = "POST"
    request.url.path = "/system/file/upload"
    request.headers = {"content-type": "application/octet-stream"}
    response = MagicMock(status_code=200)
    runtime = MagicMock()
    runtime.http_audit.operation = AsyncMock(side_effect=TimeoutError)
    with (
        patch(
            "app.middleware.audit_middleware._get_user_info",
            AsyncMock(return_value=(0, 123, "alice")),
        ),
        patch(
            "app.middleware.audit_middleware.get_authorization_runtime",
            return_value=runtime,
        ),
        patch(
            "app.middleware.audit_middleware.get_client_ip", return_value="127.0.0.1"
        ),
    ):
        result = await AuditLogMiddleware(AsyncMock()).dispatch(
            request, AsyncMock(return_value=response)
        )
    assert result is response
    runtime.http_audit.operation.assert_awaited_once()
