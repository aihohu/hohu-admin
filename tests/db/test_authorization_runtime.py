"""The composition factory refuses legacy writes before they reach PostgreSQL."""

import pytest
from sqlalchemy import delete, event, insert, select, text, update

from app.core.exceptions import BusinessException
from app.core.security import create_access_token
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.system.models.user import User
from app.modules.system.schemas.user import UpdateProfile
from tests.modules.system.test_authorization_system_candidate import (
    TENANT,
    epoch,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.mark.parametrize(
    "operation", ["update", "insert", "delete", "lock", "sql", "flush"]
)
async def test_unplanned_write_never_reaches_driver(system_sessions, operation):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    seen = []

    def observe(_conn, _cursor, sql, _params, _context, _many):
        seen.append(sql)

    async with runtime.sessions() as db:
        # Establish the connection so the journal listener is installed first.
        await db.scalar(select(User.user_id).where(User.user_id == 101))
        connection = db.sync_session.connection()
        event.listen(connection, "before_cursor_execute", observe)
        with pytest.raises(BusinessException) as caught:
            if operation == "update":
                await db.execute(
                    update(User).where(User.user_id == 101).values(nickname="leak")
                )
            elif operation == "insert":
                await db.execute(
                    insert(User).values(
                        tenant_id=0,
                        user_id=999,
                        user_name="leak",
                        hashed_password="unused",
                    )
                )
            elif operation == "delete":
                await db.execute(delete(User).where(User.user_id == 101))
            elif operation == "lock":
                await db.scalar(
                    select(User.user_id).where(User.user_id == 101).with_for_update()
                )
            elif operation == "sql":
                await db.execute(
                    text("UPDATE sys_user SET nickname='leak' WHERE user_id=101")
                )
            else:
                db.add(
                    User(
                        tenant_id=0,
                        user_id=999,
                        user_name="leak",
                        hashed_password="unused",
                    )
                )
                await db.flush()
        assert caught.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        assert seen == []
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()
        # A rollback resets transaction state, never the factory's write rule.
        with pytest.raises(BusinessException):
            await db.execute(
                update(User).where(User.user_id == 101).values(nickname="leak")
            )
        await db.rollback()
    async with plain() as db:
        assert await db.scalar(select(User.nickname).where(User.user_id == 101)) is None
        assert await db.scalar(select(User.user_id).where(User.user_id == 999)) is None
        assert await epoch(db) == 1


async def test_runtime_system_owner_commits_once_and_noop_keeps_epoch(system_sessions):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    for _ in range(2):
        await runtime.system(
            tenant=TENANT,
            actor_user_version=1,
            operation="user.profile",
            payload=UpdateProfile(nickname="protected"),
        )
    async with plain() as db:
        assert (
            await db.scalar(select(User.nickname).where(User.user_id == 101))
            == "protected"
        )
        assert await epoch(db) == 2


async def test_runtime_does_not_accept_borrowed_connection_or_factory(system_sessions):
    _, plain = system_sessions
    with pytest.raises(BusinessException):
        AuthorizationRuntime(plain)
    async with plain.kw["bind"].connect() as connection:
        with pytest.raises(BusinessException):
            AuthorizationRuntime(connection)


async def test_all_factory_owners_share_the_strict_factory(system_sessions):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    for owner in (
        runtime.gateway,
        runtime.lifecycle,
        runtime.chat,
        runtime.preview,
        runtime.transfer,
        runtime.trace,
        runtime.recovery,
        runtime.lease,
    ):
        assert owner.factory is runtime.sessions
    with pytest.raises(TypeError):
        runtime.sessions(bind=plain.kw["bind"])


async def test_auth_and_system_share_live_token_version(system_sessions):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    token = create_access_token(
        subject="101", tenant_id=0, tenant_version=1, user_version=1
    )
    identity = await runtime.current(token)
    assert identity.user_id == 101 and identity.user_version == 1
    await runtime.system(
        tenant=TENANT, actor_user_version=1, operation="user.revoke", target_id=101
    )
    with pytest.raises(BusinessException) as caught:
        await runtime.current(token)
    assert caught.value.error_code == "TOKEN_EXPIRED"
    async with plain() as db:
        assert await epoch(db) == 2
