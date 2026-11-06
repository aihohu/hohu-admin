"""Principal HTTP authentication uses the global authorization guard."""

import pytest
from sqlalchemy import event, select, update

from app.modules.platform import api, authorization_principal_candidate
from app.modules.platform.models import PlatformPrincipal
from app.modules.system.models.user_field_policy import UserFieldPolicyGlobalGuard
from tests.modules.platform.test_authorization_principal_candidate import (
    principal_sessions as principal_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_system_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    system_sessions as system_sessions,
)


@pytest.fixture
async def principal_http(http_system, principal_sessions):
    client, plain = http_system
    assert principal_sessions[1] is plain
    client._transport.app.include_router(api.router, prefix="/platform/auth")
    yield client, plain


async def test_principal_login_and_identity_use_one_owner(principal_http, monkeypatch):
    client, plain = principal_http
    commits, locks, active, passwords = [], [], set(), []
    engine = plain.kw["bind"].sync_engine
    verify = authorization_principal_candidate.verify_password

    async def password(*args):
        assert not active
        passwords.append(True)
        return await verify(*args)

    def begin(connection):
        active.add(connection)

    def commit(connection):
        active.discard(connection)
        commits.append(connection)

    def rollback(connection):
        active.discard(connection)

    def sql(_c, _cursor, statement, _params, _ctx, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    monkeypatch.setattr(authorization_principal_candidate, "verify_password", password)
    listeners = (
        ("begin", begin),
        ("commit", commit),
        ("rollback", rollback),
        ("before_cursor_execute", sql),
    )
    for name, callback in listeners:
        event.listen(engine, name, callback)
    try:
        response = await client.post(
            "/platform/auth/login",
            json={"principalName": "control-admin", "password": "Principal123!"},
        )
        assert response.status_code == 200, response.text
        assert set(response.json()["data"]) == {"token"}
        assert len(commits) == 1
        assert passwords == [True]
        assert locks and "sys_user_field_policy_global_guard" in locks[0]
        token = response.json()["data"]["token"]
        locks.clear()
        identity = await client.get(
            "/platform/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert identity.status_code == 200, identity.text
        assert identity.json()["data"]["principalId"] == "700"
        assert len(commits) == 2
        assert locks and "sys_user_field_policy_global_guard" in locks[0]
    finally:
        for name, callback in listeners:
            event.remove(engine, name, callback)
    async with plain() as db:
        assert await db.scalar(select(PlatformPrincipal.last_login_at)) is not None
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 1
        await db.execute(update(PlatformPrincipal).values(status="2"))
        await db.commit()
    stale = await client.get(
        "/platform/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert stale.status_code == 401, stale.text


@pytest.mark.parametrize(
    "name,password", [("missing", "Principal123!"), ("control-admin", "bad")]
)
async def test_principal_http_bad_credentials(principal_http, name, password):
    client, _ = principal_http
    response = await client.post(
        "/platform/auth/login", json={"principalName": name, "password": password}
    )
    assert response.status_code == 401, response.text
    assert response.json()["errorCode"] == "PLATFORM_INVALID_CREDENTIALS"
