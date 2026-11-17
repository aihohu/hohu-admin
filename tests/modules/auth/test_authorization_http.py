"""Auth HTTP contracts route through the fixed transaction owners."""

import asyncio
import re
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.util.concurrency import await_only

from app.core import authorization_io
from app.core.exceptions import BusinessException, setup_exception_handlers
from app.core.security import get_password_hash
from app.db import authorization_http, session
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.request_session import RequestSession
from app.modules.auth import api, candidate_owner, service
from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.models.user import User
from app.modules.system.service.mail_service import mail_service
from tests.modules.auth.test_auth_candidate import accounts as accounts
from tests.modules.auth.test_auth_candidate import token


@pytest.fixture
async def auth_http_engine(accounts):
    async with accounts.plain() as db:
        schema = await db.scalar(text("SELECT current_schema()"))
    assert re.fullmatch(r"testcommit_[0-9a-f]{32}", schema)
    engine = create_async_engine(
        accounts.plain.kw["bind"].url,
        connect_args={"server_settings": {"search_path": schema}},
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def auth_http(accounts, auth_http_engine, monkeypatch):
    engine = accounts.http_engine = auth_http_engine
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

    engine = accounts.http_engine.sync_engine
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


async def test_login_audit_reuses_http_pool_after_rejected_password(
    auth_http, monkeypatch
):
    client, accounts = auth_http
    engine = accounts.http_engine
    verify = candidate_owner.verify_password
    reject_seen = False
    cold_connections = []
    release = asyncio.Event()

    async def verified(password, encoded):
        nonlocal reject_seen
        matched = await verify(password, encoded)
        reject_seen = not matched
        return matched

    def unavailable_new_connection(_dialect, _record, _args, _kwargs):
        if reject_seen:
            cold_connections.append(True)
            await_only(release.wait())

    monkeypatch.setattr(candidate_owner, "verify_password", verified)
    event.listen(engine.sync_engine, "do_connect", unavailable_new_connection)
    try:
        response = await client.post(
            "/auth/token", data={"username": "alice", "password": "OldStrong42!"}
        )
        assert response.status_code == 200, response.text
        denied = await client.post(
            "/auth/login", json={"userName": "alice", "password": "WrongPassword!"}
        )
        assert denied.status_code == 401
    finally:
        event.remove(engine.sync_engine, "do_connect", unavailable_new_connection)
        release.set()
    async with accounts.plain() as db:
        logs = list(
            await db.scalars(select(SysLoginLog).order_by(SysLoginLog.login_log_id))
        )
    assert [(row.audit_scope, row.user_id, row.status) for row in logs] == [
        ("tenant", 101, "1"),
        ("tenant", None, "2"),
    ]
    assert cold_connections == []


async def test_login_audit_cold_connection_budget_fails_closed(accounts):
    engine = accounts.plain.kw["bind"]
    runtime = AuthorizationRuntime(engine)
    entered = asyncio.Event()
    release = asyncio.Event()
    writes = []

    def unavailable_connection(_dialect, _record, _args, _kwargs):
        entered.set()
        await_only(release.wait())

    def capture(_conn, _cursor, sql, _params, _context, _many):
        if sql.startswith("INSERT INTO sys_login_log"):
            writes.append(True)

    event.listen(engine.sync_engine, "do_connect", unavailable_connection)
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    pending = asyncio.create_task(
        runtime.http_audit.login(
            tenant_id=0,
            audit_scope="tenant",
            user_id=None,
            username="alice",
            ip=None,
            user_agent=None,
            status="2",
            message="Invalid credentials",
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        with pytest.raises(BusinessException) as error:
            await asyncio.wait_for(pending, timeout=3)
        assert error.value.error_code == "AUTHORIZATION_TRANSACTION_TIMEOUT"
        assert not release.is_set()
        assert writes == []
    finally:
        event.remove(engine.sync_engine, "do_connect", unavailable_connection)
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    async with accounts.plain() as db:
        assert list(await db.scalars(select(SysLoginLog))) == []
