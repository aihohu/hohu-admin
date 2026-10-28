"""Database, permission and concurrency regressions for account self service."""

import asyncio
import importlib
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import redis.asyncio as redis
from sqlalchemy import select, text

from app.core.config import settings
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.security import create_access_token, get_password_hash, verify_password
from app.db import authorization_http
from app.db.session import AsyncSessionLocal
from app.modules.auth.candidate_owner import auth_candidate_owner
from app.modules.auth.email_verification import EmailVerification
from app.modules.auth.schemas.self_service import Registration
from app.modules.auth.self_service_api import policy
from app.modules.system.api.settings import _mail_admin
from app.modules.system.models.mail_config import MailConfig
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.account_service import account_service
from app.modules.system.service.mail_service import (
    MailSettings,
    MailWrite,
    mail_service,
)
from app.modules.system.service.settings_service import settings_service
from tests.committed_database import committed_test_database as committed_test_database
from tests.tenant_helpers import create_test_tenant, tenant_context


async def test_mail_is_encrypted_preserved_cleared_and_versioned(db_session):
    initial = await mail_service.describe(db_session)
    saved = await mail_service.save(
        db_session,
        MailWrite(
            revision=initial["revision"],
            values=MailSettings(
                host="smtp.example.com",
                username="sender",
                password="smtp-app-key",
                from_email="sender@example.com",
            ),
        ),
    )
    assert saved["values"]["password"] == ""
    assert saved["configuredPassword"]
    row = await db_session.scalar(select(MailConfig))
    assert row.password_ciphertext != "smtp-app-key"
    with pytest.raises(BusinessRuleException, match="Reload"):
        await mail_service.save(
            db_session, MailWrite(revision=initial["revision"], values=MailSettings())
        )
    saved = await mail_service.save(
        db_session,
        MailWrite(revision=saved["revision"], values=MailSettings(**saved["values"])),
    )
    assert (await mail_service.config(db_session)).password == "smtp-app-key"
    saved = await mail_service.save(
        db_session,
        MailWrite(
            revision=saved["revision"],
            values=MailSettings(**saved["values"]),
            clear_password=True,
        ),
    )
    assert not saved["configuredPassword"]


def test_tenant_admin_cannot_manage_shared_smtp():
    user = SimpleNamespace(
        tenant_id=42,
        roles=[SimpleNamespace(role_code="R_SUPER", status="1", tenant_id=42)],
    )
    with pytest.raises(AuthorizationException):
        _mail_admin(user)


async def test_enabled_services_require_mail_and_duration_is_tenant_scoped(db_session):
    other = await create_test_tenant(db_session, prefix="email")
    tenant = tenant_context(tenant_id=other.tenant_id, actor_user_id=1)
    current = await settings_service.get_group(db_session, "account", tenant=tenant)
    with pytest.raises(BusinessRuleException):
        await settings_service.save_group(
            db_session,
            "account",
            {"register_enabled": True},
            revision=current["revision"],
            tenant=tenant,
        )
    await settings_service.save_group(
        db_session,
        "account",
        {"email_code_minutes": 30},
        revision=current["revision"],
        tenant=tenant,
    )
    assert (await settings_service.values(db_session, tenant_id=other.tenant_id))[
        "email_code_minutes"
    ] == 30
    assert (await settings_service.values(db_session, tenant_id=0))[
        "email_code_minutes"
    ] == 5
    with pytest.raises(BusinessRuleException):
        await policy(db_session, other.tenant_id, "register")


async def test_registration_recovery_and_all_email_writers_invalidate_trust(db_session):
    tenant = await create_test_tenant(db_session, prefix="recovery")
    payload = Registration(
        userName="newaccount",
        email="one@example.com",
        code="123456",
        password="Password123",
    )
    user = await account_service.register(db_session, tenant.tenant_id, payload, "")
    assert user.email_verified_at and not user.roles
    identity = account_service.recovery_identity(user, "one@example.com")
    assert (
        identity
        and account_service.recovery_identity(user, "other@example.com") is None
    )
    assert await account_service.find(db_session, 0, user.user_name) is None
    await account_service.reset(db_session, user, "NewPassword123")
    assert verify_password("NewPassword123", user.hashed_password)
    assert account_service.recovery_identity(user, "one@example.com") != identity
    user.status = "2"
    assert account_service.recovery_identity(user, "one@example.com") is None
    await db_session.execute(
        text(
            "UPDATE sys_user SET user_email = :email WHERE tenant_id = :tenant AND user_id = :id"
        ),
        {"email": "two@example.com", "tenant": tenant.tenant_id, "id": user.user_id},
    )
    await db_session.refresh(user)
    assert user.email_verified_at is None
    await account_service.bind(db_session, user, "three@example.com")
    await db_session.refresh(user)
    assert user.user_email == "three@example.com" and user.email_verified_at


async def test_duplicate_registration_does_not_abort_outer_transaction(db_session):
    tenant = await create_test_tenant(db_session, prefix="duplicate")
    payload = Registration(
        userName="duplicate",
        email="one@example.com",
        code="123456",
        password="Password123",
    )
    await account_service.register(db_session, tenant.tenant_id, payload, "")
    with pytest.raises(BusinessRuleException):
        await account_service.register(db_session, tenant.tenant_id, payload, "")
    assert await account_service.find(db_session, tenant.tenant_id, "duplicate")


async def test_registration_role_is_explicit_and_tenant_scoped(db_session):
    tenant = await create_test_tenant(db_session, prefix="regrole")
    role = Role(
        tenant_id=tenant.tenant_id,
        role_name="Member",
        role_code="MEMBER",
        status="1",
        data_scope="5",
        menus=[],
    )
    db_session.add(role)
    await db_session.flush()
    payload = Registration(
        userName="member",
        email="one@example.com",
        code="123456",
        password="Password123",
    )
    user = await account_service.register(
        db_session, tenant.tenant_id, payload, str(role.role_id)
    )
    await db_session.refresh(user)
    assert [r.role_id for r in user.roles] == [role.role_id]
    with pytest.raises(BusinessRuleException):
        await account_service.registration_role(db_session, 0, str(role.role_id))


async def test_unverified_historical_email_cannot_recover(db_session):
    tenant = await create_test_tenant(db_session, prefix="legacy")
    user = User(
        tenant_id=tenant.tenant_id,
        user_name="legacy",
        hashed_password=get_password_hash("Password123"),
        user_email="old@example.com",
        status="1",
    )
    db_session.add(user)
    await db_session.flush()
    assert account_service.recovery_identity(user, "old@example.com") is None
    user.email_verified_at = datetime.now(UTC)
    assert account_service.recovery_identity(user, "old@example.com")


async def test_mail_test_failure_is_recorded_without_exposing_smtp_error(
    db_session, monkeypatch
):
    current = await mail_service.describe(db_session)
    await mail_service.save(
        db_session,
        MailWrite(
            revision=current["revision"],
            values=MailSettings(
                host="smtp.example.com", from_email="sender@example.com"
            ),
        ),
    )
    monkeypatch.setattr(
        mail_service,
        "send",
        AsyncMock(side_effect=BusinessRuleException("private SMTP detail")),
    )
    result = await mail_service.test(db_session, "test@example.com")
    assert result["lastTestStatus"] == "failed"
    assert "private SMTP detail" not in str(result)


@pytest.fixture
async def codes(monkeypatch):
    module = importlib.import_module("app.modules.auth.email_verification")
    connection = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    monkeypatch.setattr(module, "redis_client", connection)
    service = EmailVerification()
    tenant_id = 9876101001
    try:
        yield service, connection, tenant_id
    finally:
        keys = [
            key
            async for key in connection.scan_iter(match=f"tenant:{tenant_id}:email-*")
        ]
        if keys:
            await connection.delete(*keys)
        await connection.aclose()


async def test_code_is_tenant_purpose_bound_and_atomically_single_use(codes):
    service, connection, tenant_id = codes
    code = await service.issue(tenant_id, "register", "alice:a@example.com", 5)
    assert (
        295
        <= await connection.ttl(
            service.key(tenant_id, "register", "alice:a@example.com")
        )
        <= 300
    )
    for other_tenant, purpose in ((tenant_id + 1, "register"), (tenant_id, "reset")):
        with pytest.raises(BusinessRuleException):
            await service.consume(other_tenant, purpose, "alice:a@example.com", code)
    results = await asyncio.gather(
        *(
            service.consume(tenant_id, "register", "alice:a@example.com", code)
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1


async def test_resend_and_wrong_attempts_do_not_reset_protection(codes):
    service, connection, tenant_id = codes
    key = service.key(tenant_id, "bind", "identity")
    original = await service.issue(tenant_id, "bind", "identity", 5)
    with pytest.raises(BusinessRuleException):
        await service.issue(tenant_id, "bind", "identity", 30)
    for _ in range(4):
        with pytest.raises(BusinessRuleException):
            await service.consume(tenant_id, "bind", "identity", "not-a-code")
    await connection.delete(key + ":cooldown")
    replacement = await service.issue(tenant_id, "bind", "identity", 30)
    assert await connection.ttl(key) > 1700
    with pytest.raises(BusinessRuleException):
        await service.consume(tenant_id, "bind", "identity", original)
    await service.consume(tenant_id, "bind", "identity", replacement)
    assert await connection.get(key + ":total") == "6"


async def test_cumulative_verification_limit_survives_resend(codes):
    service, connection, tenant_id = codes
    key = service.key(tenant_id, "reset", "limited")
    for _ in range(6):
        await connection.delete(key + ":cooldown")
        await service.issue(tenant_id, "reset", "limited", 5)
        for _ in range(5):
            with pytest.raises(BusinessRuleException):
                await service.consume(tenant_id, "reset", "limited", "wrong")
    await connection.delete(key + ":cooldown")
    code = await service.issue(tenant_id, "reset", "limited", 5)
    with pytest.raises(BusinessRuleException):
        await service.consume(tenant_id, "reset", "limited", code)


async def test_email_sending_throttle_cannot_be_bypassed_by_changing_purpose(codes):
    service, _, tenant_id = codes
    for _ in range(10):
        await service.throttle(tenant_id, "target@example.com", "127.0.0.1")
    with pytest.raises(BusinessRuleException):
        await service.throttle(tenant_id, "target@example.com", "127.0.0.2")


@pytest.fixture
async def http_account_database(committed_test_database, monkeypatch):
    monkeypatch.setattr(authorization_http, "engine", committed_test_database)
    async with AsyncSessionLocal() as db:
        yield db


async def test_http_registration_reset_and_disabled_policy(
    client, http_account_database, codes, monkeypatch
):
    db_session = http_account_database
    sent = AsyncMock()
    monkeypatch.setattr(auth_candidate_owner, "_notify", sent)
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    current = await mail_service.describe(db_session)
    await mail_service.save(
        db_session,
        MailWrite(
            revision=current["revision"],
            values=MailSettings(
                enabled=True, host="smtp.example.com", from_email="sender@example.com"
            ),
        ),
    )
    created_tenant = await create_test_tenant(db_session, prefix="httpemail")
    tenant_id = created_tenant.tenant_id
    tenant = tenant_context(tenant_id=tenant_id, actor_user_id=1)
    current = await settings_service.get_group(db_session, "account", tenant=tenant)
    await settings_service.save_group(
        db_session,
        "account",
        {
            "register_enabled": True,
            "password_reset_enabled": True,
            "email_code_minutes": 10,
        },
        revision=current["revision"],
        tenant=tenant,
    )
    await db_session.commit()
    identity = {
        "userName": "httpaccount",
        "email": "http@example.com",
        "tenantCode": created_tenant.tenant_code,
    }
    try:
        response = await client.get(
            "/auth/capabilities", params={"tenantCode": created_tenant.tenant_code}
        )
        assert response.json()["data"]["registrationEnabled"]
        response = await client.post(
            "/auth/email-code", json={**identity, "purpose": "register"}
        )
        assert response.json()["data"]["expiresIn"] == 600
        code = re.search(r"\d{6}", sent.call_args.args[3]).group()
        response = await client.post(
            "/auth/register", json={**identity, "code": code, "password": "Password123"}
        )
        assert response.json()["code"] == 200
        assert isinstance(response.json()["data"]["userId"], str)
        user = await account_service.find(db_session, tenant_id, "httpaccount")
        version = user.auth_version
        response = await client.post(
            "/auth/email-code", json={**identity, "purpose": "reset"}
        )
        assert response.json()["code"] == 200
        reset_code = re.search(r"\d{6}", sent.call_args.args[3]).group()
        response = await client.post(
            "/auth/password-reset",
            json={**identity, "code": reset_code, "password": "ChangedPassword123"},
        )
        assert response.json()["code"] == 200
        await db_session.refresh(user)
        assert user.auth_version > version
        response = await client.post(
            "/auth/password-reset",
            json={**identity, "code": reset_code, "password": "ChangedAgain123"},
        )
        assert response.json()["errorCode"] == "EMAIL_CODE_INVALID"
        client.headers["Authorization"] = "Bearer " + create_access_token(
            subject=str(user.user_id),
            tenant_id=tenant_id,
            tenant_version=created_tenant.row_version,
            user_version=user.auth_version,
        )
        binding = {"email": "replacement@example.com", "password": "ChangedPassword123"}
        response = await client.post(
            "/auth/email/code", json={**binding, "password": "WrongPassword123"}
        )
        assert response.json()["errorCode"] == "INCORRECT_OLD_PASSWORD"
        response = await client.post("/auth/email/code", json=binding)
        assert response.json()["code"] == 200
        binding_code = re.search(r"\d{6}", sent.call_args.args[3]).group()
        response = await client.put(
            "/auth/email", json={**binding, "code": binding_code}
        )
        assert response.json()["code"] == 200
        await db_session.refresh(user)
        client.headers["Authorization"] = "Bearer " + create_access_token(
            subject=str(user.user_id),
            tenant_id=tenant_id,
            tenant_version=created_tenant.row_version,
            user_version=user.auth_version,
        )
        response = await client.get("/auth/email")
        assert response.json()["data"]["verified"] is True
        assert response.json()["data"]["email"] == "replacement@example.com"
        current = await settings_service.get_group(db_session, "account", tenant=tenant)
        await settings_service.save_group(
            db_session,
            "account",
            {"register_enabled": False},
            revision=current["revision"],
            tenant=tenant,
        )
        await db_session.commit()
        response = await client.post(
            "/auth/register", json={**identity, "code": code, "password": "Password123"}
        )
        assert response.json()["errorCode"] == "ACCOUNT_SERVICE_DISABLED"
    finally:
        client.headers.pop("Authorization", None)
        _, connection, _ = codes
        # Only this test's explicitly scoped identities; never flush shared Redis.
        keys = [
            key
            async for key in connection.scan_iter(match=f"tenant:{tenant_id}:email-*")
        ]
        if keys:
            await connection.delete(*keys)
