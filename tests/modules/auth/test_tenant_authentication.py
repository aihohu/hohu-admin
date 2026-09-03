from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt
from pydantic import ValidationError
from starlette.requests import Request

from app.core.base_response import ResponseModel
from app.core.config import Settings, settings
from app.core.exceptions import AuthenticationException, AuthorizationException
from app.core.security import create_access_token, create_refresh_token
from app.modules.auth.api import login_for_docs
from app.modules.auth.schemas.auth import LoginCredentials
from app.modules.auth.service import (
    auth_service,
    get_current_tenant_context,
    get_current_user,
    refresh_access_token,
)
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User


def _scalar_result(value):
    result = MagicMock()
    result.scalars.return_value.first.return_value = value
    return result


def _tenant(*, tenant_id: int, code: str, status: str = "1") -> Tenant:
    return Tenant(
        tenant_id=tenant_id,
        tenant_code=code,
        tenant_name=code.title(),
        status=status,
        lifecycle_state="active" if status == "1" else "disabled",
        row_version=1,
    )


def _user(*, user_id: int, tenant: Tenant, name: str = "alice") -> User:
    user = User(
        user_id=user_id,
        tenant_id=tenant.tenant_id,
        user_name=name,
        hashed_password="hashed",
        status="1",
    )
    user.tenant = tenant
    return user


async def _authenticate(
    *, tenant: Tenant | None, user: User | None, credentials: LoginCredentials
):
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[_scalar_result(tenant), _scalar_result(user)]
        if tenant is not None
        else [_scalar_result(None)]
    )
    with (
        patch("app.modules.auth.service.verify_password", return_value=True),
        patch.object(auth_service, "_write_login_log", AsyncMock()),
    ):
        return await auth_service.authenticate(credentials, db)


async def test_single_mode_uses_default_tenant_and_issues_tid(monkeypatch):
    monkeypatch.setattr(settings, "TENANT_MODE", "single")
    tenant = _tenant(tenant_id=0, code="default")
    user = _user(user_id=101, tenant=tenant)

    response = await _authenticate(
        tenant=tenant,
        user=user,
        credentials=LoginCredentials(user_name="alice", password="secret"),
    )

    access = jwt.decode(
        response.data["token"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    refresh = jwt.decode(
        response.data["refreshToken"],
        settings.SECRET_KEY,
        algorithms=[settings.ALGORITHM],
    )
    assert access["tid"] == "0"
    assert refresh["tid"] == "0"


async def test_hosted_mode_normalizes_body_locator_and_supports_same_user_name(
    monkeypatch,
):
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    tenant_b = _tenant(tenant_id=22, code="tenant-b")
    tenant_b_user = _user(user_id=202, tenant=tenant_b, name="alice")

    response = await _authenticate(
        tenant=tenant_b,
        user=tenant_b_user,
        credentials=LoginCredentials(
            tenant_code=" TENANT-B ", user_name="alice", password="secret"
        ),
    )

    payload = jwt.decode(
        response.data["token"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    assert payload["sub"] == "202"
    assert payload["tid"] == "22"


async def test_hosted_mode_can_resolve_a_validated_subdomain(monkeypatch):
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    monkeypatch.setattr(settings, "TENANT_HOST_SUFFIX", "example.test")
    tenant = _tenant(tenant_id=22, code="tenant-b")
    user = _user(user_id=202, tenant=tenant)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar_result(tenant), _scalar_result(user)])

    with (
        patch("app.modules.auth.service.verify_password", return_value=True),
        patch.object(auth_service, "_write_login_log", AsyncMock()),
    ):
        response = await auth_service.authenticate(
            LoginCredentials(user_name="alice", password="secret"),
            db,
            host="tenant-b.example.test:443",
        )

    payload = jwt.decode(
        response.data["token"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    assert payload["tid"] == "22"


@pytest.mark.parametrize("failure", ["tenant", "user", "password"])
async def test_unknown_tenant_user_and_password_share_invalid_credentials_surface(
    monkeypatch, failure
):
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    tenant = None if failure == "tenant" else _tenant(tenant_id=22, code="tenant-b")
    user = (
        None
        if failure == "user" or tenant is None
        else _user(user_id=202, tenant=tenant)
    )
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[_scalar_result(tenant), _scalar_result(user)]
        if tenant is not None
        else [_scalar_result(None)]
    )

    with (
        patch(
            "app.modules.auth.service.verify_password",
            return_value=failure != "password",
        ) as verify,
        patch.object(auth_service, "_write_login_log", AsyncMock()),
    ):
        with pytest.raises(AuthenticationException) as exc_info:
            await auth_service.authenticate(
                LoginCredentials(
                    tenant_code="tenant-b",
                    user_name="alice",
                    password="secret",
                ),
                db,
            )

    assert exc_info.value.error_code == "INVALID_CREDENTIALS"
    assert exc_info.value.message == "账号或密码错误"
    verify.assert_called_once()


async def test_disabled_tenant_uses_invalid_credentials_during_login(monkeypatch):
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    tenant = _tenant(tenant_id=22, code="tenant-b", status="2")
    user = _user(user_id=202, tenant=tenant)

    with pytest.raises(AuthenticationException) as exc_info:
        await _authenticate(
            tenant=tenant,
            user=user,
            credentials=LoginCredentials(
                tenant_code="tenant-b", user_name="alice", password="secret"
            ),
        )

    assert exc_info.value.error_code == "INVALID_CREDENTIALS"


async def test_hosted_login_is_fail_closed_before_release_gate(monkeypatch):
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", False)
    db = AsyncMock()

    with pytest.raises(AuthenticationException) as exc_info:
        await auth_service.resolve_login_tenant(
            LoginCredentials(
                tenant_code="tenant-b", user_name="alice", password="secret"
            ),
            db,
        )

    assert exc_info.value.error_code == "INVALID_CREDENTIALS"
    db.execute.assert_not_awaited()


async def test_single_mode_requires_the_canonical_default_tenant_id(monkeypatch):
    monkeypatch.setattr(settings, "TENANT_MODE", "single")
    noncanonical = _tenant(tenant_id=22, code="default")
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar_result(noncanonical))

    with pytest.raises(AuthenticationException) as exc_info:
        await auth_service.resolve_login_tenant(LoginCredentials(), db)

    assert exc_info.value.error_code == "INVALID_CREDENTIALS"


def test_login_schema_rejects_forged_tenant_fields():
    with pytest.raises(ValidationError):
        LoginCredentials.model_validate(
            {
                "userName": "alice",
                "password": "secret",
                "tenantId": "22",
            }
        )


def test_hosted_login_release_gate_cannot_be_enabled_from_settings():
    with pytest.raises(ValidationError):
        Settings(
            DATABASE_URL=settings.DATABASE_URL,
            SECRET_KEY=settings.SECRET_KEY,
            TENANT_HOSTED_LOGIN_ENABLED=True,  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError):
        Settings(
            DATABASE_URL=settings.DATABASE_URL,
            SECRET_KEY=settings.SECRET_KEY,
            TENANT_MODE="hosted",
        )


async def test_docs_login_forwards_host_locator_to_authentication():
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/token",
            "headers": [(b"host", b"tenant-b.example.test")],
        }
    )
    form = SimpleNamespace(username="alice", password="secret")
    db = AsyncMock()
    authenticate = AsyncMock(
        return_value=ResponseModel.success(data={"token": "access"})
    )

    with patch.object(auth_service, "authenticate", authenticate):
        response = await login_for_docs(request=request, form=form, db=db)

    assert response == {"access_token": "access", "token_type": "bearer"}
    assert authenticate.await_args.kwargs["host"] == "tenant-b.example.test"


@pytest.mark.parametrize("password", [None, ""])
async def test_password_login_rejects_missing_password_without_server_error(
    monkeypatch, password
):
    monkeypatch.setattr(settings, "TENANT_MODE", "single")
    tenant = _tenant(tenant_id=0, code="default")
    user = _user(user_id=101, tenant=tenant)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_scalar_result(tenant), _scalar_result(user)])

    with (
        patch("app.modules.auth.service.verify_password") as verify,
        patch.object(auth_service, "_write_login_log", AsyncMock()),
        pytest.raises(AuthenticationException) as exc_info,
    ):
        await auth_service.authenticate(
            LoginCredentials(user_name="alice", password=password), db
        )

    verify.assert_not_called()
    assert exc_info.value.error_code == "INVALID_CREDENTIALS"


async def test_current_user_rejects_tid_mismatch_and_old_token_without_tid():
    tenant = _tenant(tenant_id=0, code="default")
    user = _user(user_id=101, tenant=tenant)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar_result(user))

    mismatch = create_access_token(subject="101", tenant_id=9)
    with patch(
        "app.modules.auth.service._is_blacklisted", AsyncMock(return_value=False)
    ):
        with pytest.raises(AuthenticationException) as mismatch_error:
            await get_current_user(token=mismatch, db=db)
    assert mismatch_error.value.error_code == "TOKEN_EXPIRED"

    old_payload = {
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "sub": "101",
        "type": "access",
    }
    old_token = jwt.encode(
        old_payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )
    with patch(
        "app.modules.auth.service._is_blacklisted", AsyncMock(return_value=False)
    ):
        with pytest.raises(AuthenticationException) as old_error:
            await get_current_user(token=old_token, db=db)
    assert old_error.value.error_code == "TOKEN_EXPIRED"


async def test_current_user_rejects_non_scalar_signed_identity_claims():
    payload = {
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "sub": "101",
        "tid": [0],
        "type": "access",
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    db = AsyncMock()

    with patch(
        "app.modules.auth.service._is_blacklisted", AsyncMock(return_value=False)
    ):
        with pytest.raises(AuthenticationException) as exc_info:
            await get_current_user(token=token, db=db)

    assert exc_info.value.error_code == "TOKEN_EXPIRED"
    db.execute.assert_not_awaited()


async def test_current_user_rejects_disabled_tenant():
    tenant = _tenant(tenant_id=22, code="tenant-b", status="2")
    user = _user(user_id=202, tenant=tenant)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar_result(user))
    token = create_access_token(subject="202", tenant_id=22)

    with patch(
        "app.modules.auth.service._is_blacklisted", AsyncMock(return_value=False)
    ):
        with pytest.raises(AuthorizationException) as exc_info:
            await get_current_user(token=token, db=db)

    assert exc_info.value.error_code == "TENANT_DISABLED"


async def test_current_user_binds_the_canonical_tenant_context():
    tenant = _tenant(tenant_id=22, code="tenant-b")
    user = _user(user_id=202, tenant=tenant)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar_result(user))
    token = create_access_token(subject="202", tenant_id=22)

    with patch(
        "app.modules.auth.service._is_blacklisted", AsyncMock(return_value=False)
    ):
        principal = await get_current_user(token=token, db=db)

    context = await get_current_tenant_context(current_user=principal)
    assert context.tenant_id == 22
    assert context.tenant_code == "tenant-b"
    assert context.actor_user_id == 202
    assert context.source == "access_token"


async def test_refresh_rejects_tid_mismatch(monkeypatch):
    tenant = _tenant(tenant_id=0, code="default")
    user = _user(user_id=101, tenant=tenant)
    token = create_refresh_token(subject="101", tenant_id=9)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_scalar_result(user))

    class _SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(
        "app.modules.auth.service.AsyncSessionLocal", lambda: _SessionContext()
    )

    with pytest.raises(AuthenticationException) as exc_info:
        await refresh_access_token(token)

    assert exc_info.value.error_code == "TOKEN_EXPIRED"
