"""Auth HTTP contracts route through the fixed transaction owners."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core import authorization_io
from app.core.exceptions import setup_exception_handlers
from app.core.security import get_password_hash
from app.db import authorization_http, session
from app.db.request_session import RequestSession
from app.modules.auth import api, candidate_owner, service
from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.models.user import User
from app.modules.system.service.mail_service import mail_service
from tests.modules.auth.test_auth_candidate import accounts as accounts
from tests.modules.auth.test_auth_candidate import token


@pytest.fixture
async def auth_http(accounts, monkeypatch):
    engine = accounts.plain.kw["bind"]
    factory = async_sessionmaker(
        engine, class_=RequestSession, expire_on_commit=False, autoflush=False
    )
    monkeypatch.setattr(session, "AsyncSessionLocal", factory)
    monkeypatch.setattr(service, "AsyncSessionLocal", factory)
    monkeypatch.setattr(authorization_http, "engine", engine)
    monkeypatch.setattr(service, "_is_blacklisted", AsyncMock(return_value=False))
    monkeypatch.setattr(
        candidate_owner, "hash_password", authorization_io.hash_password
    )
    monkeypatch.setattr(
        candidate_owner, "verify_password", authorization_io.verify_password
    )
    monkeypatch.setattr(
        candidate_owner.email_verification, "issue", AsyncMock(return_value="123456")
    )
    monkeypatch.setattr(candidate_owner.email_verification, "throttle", AsyncMock())
    monkeypatch.setattr(mail_service, "send", AsyncMock())
    async with accounts.plain() as db:
        await db.execute(
            update(User)
            .where(User.user_id == 101)
            .values(hashed_password=get_password_hash("OldStrong42!"))
        )
        await db.commit()
    app = FastAPI()
    setup_exception_handlers(app)
    app.include_router(api.router, prefix="/auth")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token(accounts)}"},
    ) as client:
        yield client, accounts


@pytest.mark.parametrize(
    "path,payload",
    [
        ("login", {"userName": "alice", "password": "OldStrong42!"}),
        ("refreshToken", None),
        (
            "register",
            {
                "userName": "created",
                "email": "created@example.com",
                "password": "NewStrong42!",
                "code": "123456",
            },
        ),
        (
            "password-reset",
            {
                "userName": "alice",
                "email": "alice@example.com",
                "password": "NewStrong42!",
                "code": "123456",
            },
        ),
        (
            "email-code",
            {
                "purpose": "register",
                "userName": "created",
                "email": "created@example.com",
            },
        ),
        ("email/code", {"email": "new@example.com", "password": "OldStrong42!"}),
        (
            "email",
            {"email": "new@example.com", "password": "OldStrong42!", "code": "123456"},
        ),
    ],
)
async def test_auth_http_uses_global_barrier_and_preserves_response(
    auth_http, path, payload
):
    client, accounts = auth_http
    locks = []

    def record(_conn, _cursor, sql, _params, _context, _many):
        if "FOR UPDATE" in sql or "FOR SHARE" in sql:
            locks.append(sql)

    engine = accounts.plain.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = await client.request(
            "PUT" if path == "email" else "POST",
            "/auth/" + path,
            json=payload
            if payload is not None
            else {"refreshToken": token(accounts, refresh=True)},
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 200
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    if path in {"login", "refreshToken"}:
        assert set(body["data"]) == {"token", "refreshToken"}
    if path == "register":
        assert isinstance(body["data"]["userId"], str)
    if path in {"email-code", "email/code"}:
        assert body["data"] == {"expiresIn": 300, "retryAfter": 60}


async def test_login_audit_and_swagger_contract(auth_http):
    client, accounts = auth_http
    response = await client.post(
        "/auth/token", data={"username": "alice", "password": "OldStrong42!"}
    )
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"access_token", "token_type"}
    assert response.json()["token_type"] == "bearer"
    denied = await client.post(
        "/auth/login", json={"userName": "alice", "password": "WrongPassword!"}
    )
    assert denied.status_code == 401
    async with accounts.plain() as db:
        logs = list(
            await db.scalars(select(SysLoginLog).order_by(SysLoginLog.login_log_id))
        )
        assert [(row.audit_scope, row.user_id, row.status) for row in logs] == [
            ("tenant", 101, "1"),
            ("tenant", None, "2"),
        ]
