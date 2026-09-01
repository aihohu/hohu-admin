"""Phase 2 deterministic authorization lock protocol tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED
from app.core.exceptions import BusinessRuleException
from app.core.id_generator import next_id
from app.db.session import engine
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_lock import authorization_lock_service
from tests.tenant_helpers import tenant_context


async def test_lock_targets_are_deduplicated_and_returned_in_global_order(
    db_session: AsyncSession,
) -> None:
    marker = next_id()
    roles = [
        Role(
            tenant_id=0,
            role_id=next_id(),
            role_name=f"lock-role-{marker}-{index}",
            role_code=f"R_LOCK_{marker}_{index}",
            status=STATUS_ENABLED,
        )
        for index in range(2)
    ]
    depts = [
        Dept(
            tenant_id=0,
            dept_id=next_id(),
            dept_name=f"lock-dept-{marker}-{index}",
            ancestors="0",
            order_num=index,
            status=STATUS_ENABLED,
        )
        for index in range(2)
    ]
    users = [
        User(
            tenant_id=0,
            user_id=next_id(),
            user_name=f"lock-user-{marker}-{index}",
            nickname="lock user",
            hashed_password="x",
            status=STATUS_ENABLED,
        )
        for index in range(2)
    ]
    db_session.add_all([*roles, *depts, *users])
    await db_session.flush()

    locked = await authorization_lock_service.lock_targets(
        db_session,
        role_ids=[roles[1].role_id, roles[0].role_id, roles[1].role_id],
        dept_ids=[depts[1].dept_id, depts[0].dept_id],
        user_ids=[users[1].user_id, users[0].user_id, users[0].user_id],
        tenant=tenant_context(tenant_id=0),
    )

    assert locked.role_ids == tuple(sorted(role.role_id for role in roles))
    assert locked.dept_ids == tuple(sorted(dept.dept_id for dept in depts))
    assert locked.user_ids == tuple(sorted(user.user_id for user in users))


async def test_missing_lock_target_fails_closed(db_session: AsyncSession) -> None:
    with pytest.raises(BusinessRuleException) as exc_info:
        await authorization_lock_service.lock_targets(
            db_session,
            role_ids=[next_id()],
            dept_ids=[],
            user_ids=[],
            tenant=tenant_context(tenant_id=0),
        )

    assert exc_info.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"


async def test_migration_advisory_lock_uses_a_bound_constant() -> None:
    db = MagicMock()
    db.execute = AsyncMock()

    await authorization_lock_service.lock_authorization_migration(db)

    statement, parameters = db.execute.await_args.args
    assert "pg_advisory_xact_lock(:lock_key)" in str(statement)
    assert parameters == {
        "lock_key": authorization_lock_service.MIGRATION_ADVISORY_LOCK_KEY
    }


async def test_session_migration_lock_uses_bound_lock_and_unlock_calls() -> None:
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.scalar = AsyncMock(return_value=True)

    await authorization_lock_service.lock_authorization_migration_session(connection)
    await authorization_lock_service.unlock_authorization_migration_session(connection)

    lock_statement, lock_parameters = connection.execute.await_args.args
    unlock_statement, unlock_parameters = connection.scalar.await_args.args
    assert "pg_advisory_lock(:lock_key)" in str(lock_statement)
    assert "pg_advisory_unlock(:lock_key)" in str(unlock_statement)
    assert (
        lock_parameters
        == unlock_parameters
        == {"lock_key": authorization_lock_service.MIGRATION_ADVISORY_LOCK_KEY}
    )


async def test_session_migration_lock_survives_transaction_end() -> None:
    lock_key = authorization_lock_service.MIGRATION_ADVISORY_LOCK_KEY
    try:
        async with engine.connect() as owner, engine.connect() as contender:
            try:
                await authorization_lock_service.lock_authorization_migration_session(
                    owner
                )
                await owner.commit()

                acquired_while_held = await contender.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": lock_key},
                )
                assert acquired_while_held is False

                await authorization_lock_service.unlock_authorization_migration_session(
                    owner
                )
                await owner.commit()
                acquired_after_release = await contender.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": lock_key},
                )
                assert acquired_after_release is True
            finally:
                await contender.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": lock_key},
                )
                await contender.commit()
    finally:
        await engine.dispose()
