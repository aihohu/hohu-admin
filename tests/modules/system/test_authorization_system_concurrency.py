"""Deterministic PostgreSQL interleavings for candidate creation."""

import asyncio

import pytest
from sqlalchemy import event, insert, select

from app.core.exceptions import BusinessException, DuplicateException
from app.db.authorization_system_owner import system_candidate_owner
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.user import UserCreate
from tests.modules.system.test_authorization_system_candidate import (
    TENANT,
    epoch,
    run,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def member_role(plain):
    async with plain() as db:
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=301,
                role_name="Member",
                role_code="R_USER",
                data_scope="5",
                status="1",
            )
        )
        await db.commit()


def user():
    return UserCreate(user_name="plannedunique", password="TestStrong123!", status="1")


async def test_old_writer_unique_conflict_waits_for_role_before_insert(system_sessions):
    factory, plain = system_sessions
    await member_role(plain)
    role_wait = asyncio.Event()
    candidate_sql = []
    candidate = None

    def record(_c, _cursor, statement, _p, _ctx, _many):
        if asyncio.current_task() is candidate:
            candidate_sql.append(statement)
            if "sys_role" in statement and "FOR UPDATE" in statement:
                role_wait.set()

    async def create():
        async with factory() as db:
            with pytest.raises(BusinessException) as failure:
                await system_candidate_owner.execute(
                    db,
                    tenant=TENANT,
                    actor_user_version=1,
                    operation="user.create",
                    payload=user(),
                )
            assert failure.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"

    engine = plain.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with plain() as blocker:
            await blocker.scalar(
                select(Role.role_id).where(Role.role_id == 301).with_for_update()
            )
            await blocker.execute(
                insert(User).values(
                    tenant_id=0,
                    user_id=501,
                    user_name="plannedunique",
                    hashed_password="fixture-only",
                    status="1",
                )
            )
            candidate = asyncio.create_task(create())
            async with asyncio.timeout(8):
                await role_wait.wait()
                assert not candidate.done()
                assert not any(
                    sql.startswith("INSERT INTO sys_user ") for sql in candidate_sql
                )
                await blocker.commit()
                await candidate
        async with plain() as db:
            assert await epoch(db) == 1
    finally:
        event.remove(engine, "before_cursor_execute", record)
        if candidate is not None:
            if not candidate.done():
                candidate.cancel()
            await asyncio.gather(candidate, return_exceptions=True)


async def test_candidate_barrier_held_through_commit_and_duplicate_does_not_bump(
    system_sessions,
):
    factory, plain = system_sessions
    await member_role(plain)
    waiting = asyncio.Event()
    contender = None

    def record(_c, _cursor, statement, _p, _ctx, _many):
        if (
            asyncio.current_task() is contender
            and "sys_user_field_policy_state" in statement
            and "FOR UPDATE" in statement
        ):
            waiting.set()

    async def second():
        async with factory() as db:
            with pytest.raises(DuplicateException):
                await system_candidate_owner.execute(
                    db,
                    tenant=TENANT,
                    actor_user_version=1,
                    operation="user.create",
                    payload=user(),
                )

    engine = plain.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with factory() as first:
            await run(first, "user.create", user())
            contender = asyncio.create_task(second())
            async with asyncio.timeout(8):
                await waiting.wait()
                assert not contender.done()
                await first.commit()
                await contender
        async with plain() as db:
            assert await epoch(db) == 2
            assert (
                len(
                    (
                        await db.scalars(
                            select(User.user_id).where(
                                User.user_name == "plannedunique"
                            )
                        )
                    ).all()
                )
                == 1
            )
    finally:
        event.remove(engine, "before_cursor_execute", record)
        if contender is not None:
            if not contender.done():
                contender.cancel()
            await asyncio.gather(contender, return_exceptions=True)
