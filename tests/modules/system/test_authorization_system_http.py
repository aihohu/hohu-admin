"""Real HTTP/JWT requests use committed fixtures and fresh owner connections."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, insert, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.exceptions import BusinessException, setup_exception_handlers
from app.core.security import create_access_token, get_password_hash
from app.db import authorization_http, authorization_system_owner, session
from app.db.base import user_roles
from app.db.request_session import RequestSession
from app.modules.auth import service
from app.modules.system.api import dept, role, user
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.fixture
async def http_system(system_sessions, monkeypatch):
    _, plain = system_sessions
    engine = plain.kw["bind"]
    monkeypatch.setattr(authorization_http, "engine", engine)
    monkeypatch.setattr(
        session,
        "AsyncSessionLocal",
        async_sessionmaker(
            engine,
            class_=RequestSession,
            expire_on_commit=False,
            autoflush=False,
        ),
    )
    monkeypatch.setattr(service, "_is_blacklisted", AsyncMock(return_value=False))
    app = FastAPI()
    setup_exception_handlers(app)
    app.include_router(user.router, prefix="/user")
    app.include_router(role.router, prefix="/role")
    app.include_router(dept.router, prefix="/dept")
    token = create_access_token(
        subject="101", tenant_id=0, tenant_version=1, user_version=1
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client, plain


async def test_profile_http_commits_once_and_invalidates_epoch(http_system):
    client, plain = http_system
    commits = []

    def record(connection):
        commits.append(connection)

    event.listen(plain.kw["bind"].sync_engine, "commit", record)
    try:
        response = await client.put("/user/profile", json={"nickname": "HTTP owner"})
    finally:
        event.remove(plain.kw["bind"].sync_engine, "commit", record)
    assert response.status_code == 200, response.text
    assert response.json()["code"] == 200
    assert len(commits) == 1
    async with plain() as db:
        assert (
            await db.scalar(select(User.nickname).where(User.user_id == 101))
            == "HTTP owner"
        )
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 2
        )


async def test_profile_rechecks_token_after_initial_auth(http_system):
    client, plain = http_system
    original = service.get_current_user

    async def changed(
        token=service.Depends(service.oauth2_scheme), db=service.Depends(session.get_db)
    ):
        actor = await original(token, db)
        async with plain() as revoke:
            await revoke.execute(
                update(User).where(User.user_id == 101).values(auth_version=2)
            )
            await revoke.commit()
        return actor

    client._transport.app.dependency_overrides[original] = changed
    response = await client.put("/user/profile", json={"nickname": "must reject"})
    assert response.status_code == 401, response.text
    async with plain() as db:
        assert await db.scalar(select(User.nickname).where(User.user_id == 101)) is None


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("POST", "/dept/add", {"deptName": "Created", "status": "1"}),
        ("PUT", "/dept/401", {"deptName": "Renamed"}),
        ("PUT", "/dept/401/move", {"newParentId": "402"}),
        ("PUT", "/dept/401/users", {"userIds": ["501"]}),
        ("DELETE", "/dept/401", None),
        ("POST", "/dept/batch-delete", [401, 402]),
        (
            "POST",
            "/role/add",
            {
                "roleName": "Created",
                "roleCode": "HTTP",
                "status": "1",
                "dataScope": "5",
            },
        ),
        ("PUT", "/role/301", {"roleName": "Renamed"}),
        ("PUT", "/role/menu/301", [1001]),
        ("DELETE", "/role/301", None),
        ("POST", "/role/batch-delete", [301]),
        (
            "POST",
            "/user/add",
            {
                "userName": "created",
                "password": "TestStrong123!",
                "status": "1",
                "roleIds": ["301"],
            },
        ),
        (
            "PUT",
            "/user/501",
            {"userName": "target", "nickname": "Renamed", "status": "1"},
        ),
        ("PUT", "/user/501/roles", {"roleIds": ["301"]}),
        (
            "PUT",
            "/user/501/departments",
            {"deptAssignments": [{"deptId": "401", "isPrimary": True}]},
        ),
        ("PUT", "/user/501/reset-password", {"newPassword": "NextStrong123!"}),
        ("PUT", "/user/501/revoke-sessions", None),
        (
            "PUT",
            "/user/change-password",
            {"oldPassword": "TestStrong123!", "newPassword": "NextStrong123!"},
        ),
    ],
)
async def test_http_system_writers_have_one_owner_and_epoch(
    http_system, method, path, payload
):
    client, plain = http_system
    async with plain() as setup:
        password = get_password_hash("TestStrong123!")
        await setup.execute(
            update(User).where(User.user_id == 101).values(hashed_password=password)
        )
        await setup.execute(
            insert(User).values(
                tenant_id=0,
                user_id=501,
                user_name="target",
                status="1",
                hashed_password=password,
            )
        )
        await setup.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=301,
                role_name="Member",
                role_code="R_USER",
                status="1",
                data_scope="5",
            )
        )
        if path == "/user/501/roles":
            await setup.execute(
                insert(Role).values(
                    tenant_id=0,
                    role_id=302,
                    role_name="Previous",
                    role_code="PREVIOUS",
                    status="1",
                    data_scope="5",
                )
            )
            await setup.execute(
                insert(user_roles).values(tenant_id=0, user_id=501, role_id=302)
            )
        for did in (401, 402):
            await setup.execute(
                insert(Dept).values(
                    tenant_id=0,
                    dept_id=did,
                    dept_name=f"Team {did}",
                    ancestors="0",
                    status="1",
                )
            )
        await setup.commit()
    commits = []
    locks = []

    def committed(connection):
        commits.append(connection)

    def sql(_connection, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    event.listen(plain.kw["bind"].sync_engine, "commit", committed)
    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", sql)
    try:
        response = await client.request(method, path, json=payload)
    finally:
        event.remove(plain.kw["bind"].sync_engine, "commit", committed)
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", sql)
    assert response.status_code == 200, response.text
    assert response.json()["code"] == 200, response.text
    assert len(commits) == 1
    assert "sys_user_field_policy_global_guard" in locks[0]
    assert "sys_user_field_policy_state" in locks[1]
    async with plain() as observer:
        assert (
            await observer.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 2
        )


async def test_http_unknown_ack_never_replays_or_commits_request_session(
    http_system, monkeypatch
):
    client, plain = http_system
    original = authorization_system_owner.commit_authorization_session
    calls = []

    async def unknown(db, effects):
        calls.append(1)
        await original(db, effects)
        raise BusinessException(
            503, "Completion unknown", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )

    monkeypatch.setattr(
        authorization_system_owner, "commit_authorization_session", unknown
    )
    response = await client.put("/user/profile", json={"nickname": "Committed once"})
    assert response.status_code == 503, response.text
    assert response.json()["errorCode"] == "AUTHORIZATION_COMMIT_UNKNOWN"
    assert calls == [1]
    async with plain() as observer:
        assert (
            await observer.scalar(select(User.nickname).where(User.user_id == 101))
            == "Committed once"
        )
        assert (
            await observer.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 2
        )


async def test_cancelled_http_lock_wait_releases_owner_and_can_serve_next_request(
    http_system, monkeypatch
):
    client, plain = http_system
    entered = asyncio.Event()
    original = authorization_http.release_request_reads

    async def released(db):
        await original(db)
        entered.set()

    monkeypatch.setattr(authorization_http, "release_request_reads", released)
    async with plain() as blocker:
        await blocker.execute(
            select(UserFieldPolicyState)
            .where(UserFieldPolicyState.tenant_id == 0)
            .with_for_update()
        )
        task = asyncio.create_task(
            client.put("/user/profile", json={"nickname": "Cancelled"})
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.sleep(0.05)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await blocker.rollback()
    async with plain() as observer:
        assert (
            await observer.scalar(select(User.nickname).where(User.user_id == 101))
            is None
        )
    response = await client.put("/user/profile", json={"nickname": "After cancel"})
    assert response.status_code == 200, response.text
