"""Fixed production read owners reauthenticate under shared G/B."""

import asyncio

import pytest
from sqlalchemy import insert, update

from app.core.exceptions import BusinessException
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.authorization_user_read_owner import user_read_owner
from app.modules.system.models.user import User
from app.modules.system.schemas.user import UpdateProfile, UserQuery
from app.modules.system.service.user_read_service import user_read_service
from tests.modules.system.test_authorization_system_candidate import (
    TENANT,
)
from tests.modules.system.test_authorization_system_candidate import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)


async def test_list_returns_projected_values_and_ends_transaction(system_sessions):
    factory, plain = system_sessions
    async with plain() as setup:
        await setup.execute(
            update(User)
            .where(User.user_id == 101)
            .values(user_phone="13812341234", user_email="ab@example.com")
        )
        await setup.commit()
    async with factory() as db:
        result = await user_read_owner.execute(
            db, tenant=TENANT, actor_user_version=1, operation="list", query=UserQuery()
        )
        assert not db.in_transaction()
    assert result["total"] == 1
    record = result["records"][0]
    assert record["userId"] == "101" and record["userPhone"] == "138****1234"
    assert "hashedPassword" not in record
    assert "not-a-credential" not in str(result)


async def test_profile_preserves_null_and_rejects_stale_actor(system_sessions):
    factory, plain = system_sessions
    async with factory() as db:
        result = await user_read_owner.execute(
            db, tenant=TENANT, actor_user_version=1, operation="profile"
        )
        assert result["userPhone"] is None
    async with plain() as setup:
        await setup.execute(
            update(User).where(User.user_id == 101).values(auth_version=2)
        )
        await setup.commit()
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await user_read_owner.execute(
                db, tenant=TENANT, actor_user_version=1, operation="profile"
            )
        assert error.value.error_code == "INVALID_CREDENTIALS"
        assert not db.in_transaction()


async def test_list_uses_existing_function_permission_and_tenant_scope(system_sessions):
    factory, plain = system_sessions
    async with plain() as setup:
        await setup.execute(
            insert(User).values(
                tenant_id=1,
                user_id=103,
                user_name="outside",
                hashed_password="unused",
                status="1",
            )
        )
        await setup.commit()
    async with factory() as db:
        result = await user_read_owner.execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation="list",
            query=UserQuery(user_name="outside"),
        )
        assert result["total"] == 0 and result["records"] == []


async def test_shared_projection_barrier_holds_a_real_writer_until_dto_is_ready(
    system_sessions, monkeypatch
):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    projected, release = asyncio.Event(), asyncio.Event()
    original = user_read_service.user_list

    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)
        projected.set()
        await release.wait()
        return result

    monkeypatch.setattr(user_read_service, "user_list", paused)
    reader = asyncio.create_task(
        runtime.user_read(
            tenant=TENANT, actor_user_version=1, operation="list", query=UserQuery()
        )
    )
    writer = None
    try:
        await asyncio.wait_for(projected.wait(), 3)
        writer = asyncio.create_task(
            runtime.system(
                tenant=TENANT,
                actor_user_version=1,
                operation="user.profile",
                payload=UpdateProfile(nickname="after"),
            )
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(writer), 0.15)
        release.set()
        before = await asyncio.wait_for(reader, 3)
        await asyncio.wait_for(writer, 3)
        assert before["records"][0]["nickname"] is None
        profile = await runtime.user_read(
            tenant=TENANT, actor_user_version=1, operation="profile"
        )
        assert profile["nickname"] == "after"
    finally:
        release.set()
        for task in (reader, writer):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (reader, writer) if task is not None),
            return_exceptions=True,
        )


async def test_cancelled_projection_releases_barriers_and_preserves_cancellation(
    system_sessions, monkeypatch
):
    _, plain = system_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])
    entered = asyncio.Event()

    async def paused(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(user_read_service, "user_list", paused)
    task = asyncio.create_task(
        runtime.user_read(
            tenant=TENANT, actor_user_version=1, operation="list", query=UserQuery()
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(
            runtime.system(
                tenant=TENANT,
                actor_user_version=1,
                operation="user.profile",
                payload=UpdateProfile(nickname="after"),
            ),
            3,
        )
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
