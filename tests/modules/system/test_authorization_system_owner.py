"""Real candidate owner completion and cache ordering."""

import asyncio

import pytest
from sqlalchemy import select, update

from app.core.exceptions import BusinessException
from app.core.security import get_password_hash, verify_password
from app.db.authorization_system_owner import system_candidate_owner
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.schemas.user import ChangePassword, UpdateProfile, UserUpdate
from tests.modules.system.test_authorization_system_candidate import (
    TENANT,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_owner_commits_before_auxiliary_cache_and_tolerates_cache_failure(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions
    calls = []

    async def delete(key):
        async with plain() as observer:
            assert (
                await observer.scalar(select(User.nickname).where(User.user_id == 101))
                == "committed"
            )
            assert (
                await observer.scalar(
                    select(UserFieldPolicyState.epoch).where(
                        UserFieldPolicyState.tenant_id == 0
                    )
                )
                == 2
            )
        calls.append(key)
        raise ConnectionError("auxiliary cache unavailable")

    monkeypatch.setattr("app.db.authorization_system_owner.redis_client.delete", delete)
    async with factory() as db:
        await system_candidate_owner.execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation="user.update",
            target_id=101,
            payload=UserUpdate(user_name="renamed", nickname="committed", status="1"),
        )
        assert not db.in_transaction()
    assert len(calls) == 1


async def test_owner_failed_business_rolls_back_and_discards_effects(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions
    calls = []

    async def delete(key):
        calls.append(key)

    monkeypatch.setattr("app.db.authorization_system_owner.redis_client.delete", delete)
    async with factory() as db:
        with pytest.raises(BusinessException):
            await system_candidate_owner.execute(
                db,
                tenant=TENANT,
                actor_user_version=999,
                operation="user.profile",
                payload=UpdateProfile(nickname="never"),
            )
        assert not db.in_transaction()
    assert calls == []
    async with plain() as db:
        assert await db.scalar(select(User.nickname).where(User.user_id == 101)) is None


async def test_owner_refuses_borrowed_session_without_rolling_it_back(system_sessions):
    factory, _ = system_sessions
    async with factory() as db:
        await db.scalar(select(User.user_id).where(User.user_id == 101))
        with pytest.raises(BusinessException) as error:
            await system_candidate_owner.execute(
                db,
                tenant=TENANT,
                actor_user_version=1,
                operation="user.profile",
                payload=UpdateProfile(nickname="never"),
            )
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        assert db.in_transaction()
        await db.rollback()


async def test_owner_preserves_cancellation_when_cleanup_ack_is_unknown(
    system_sessions, monkeypatch
):
    factory, _ = system_sessions
    cleaned = []

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    async def failed_cleanup(*_args, **_kwargs):
        cleaned.append(True)
        raise BusinessException(
            503, "unknown", error_code="AUTHORIZATION_ROLLBACK_UNKNOWN"
        )

    monkeypatch.setattr(
        "app.db.authorization_system_owner.system_authorization_candidate.execute",
        cancelled,
    )
    monkeypatch.setattr(
        "app.db.authorization_system_owner.rollback_authorization_session",
        failed_cleanup,
    )
    async with factory() as db:
        with pytest.raises(asyncio.CancelledError):
            await system_candidate_owner.execute(
                db,
                tenant=TENANT,
                actor_user_version=1,
                operation="user.profile",
                payload=UpdateProfile(nickname="never"),
            )
    assert cleaned == [True]


@pytest.mark.parametrize("revoke_during_hash", [False, True])
async def test_password_preparation_releases_transaction_and_revalidates(
    system_sessions, monkeypatch, revoke_during_hash
):
    factory, plain = system_sessions
    original = get_password_hash("Before123!")
    replacement = get_password_hash("After123!")
    async with plain() as setup:
        await setup.execute(
            update(User).where(User.user_id == 101).values(hashed_password=original)
        )
        await setup.commit()
    async with factory() as db:

        async def hash_outside_barrier(_password):
            assert not db.in_transaction()
            assert db._authorization_journal.phase == "idle"
            if revoke_during_hash:
                async with plain() as changer:
                    await changer.execute(
                        update(User).where(User.user_id == 101).values(auth_version=2)
                    )
                    await changer.commit()
            return replacement

        monkeypatch.setattr(
            "app.modules.system.service.authorization_system_candidate.hash_password",
            hash_outside_barrier,
        )
        args = {
            "tenant": TENANT,
            "actor_user_version": 1,
            "operation": "user.change_password",
            "target_id": 102,
            "payload": ChangePassword(
                old_password="Before123!", new_password="After123!"
            ),
        }
        if revoke_during_hash:
            with pytest.raises(BusinessException) as failure:
                await system_candidate_owner.execute(db, **args)
            assert failure.value.error_code == "INVALID_CREDENTIALS"
        else:
            await system_candidate_owner.execute(db, **args)
    async with plain() as observer:
        password = await observer.scalar(
            select(User.hashed_password).where(User.user_id == 101)
        )
        assert verify_password(
            "Before123!" if revoke_during_hash else "After123!", password
        )
        assert await observer.scalar(
            select(UserFieldPolicyState.epoch).where(
                UserFieldPolicyState.tenant_id == 0
            )
        ) == (1 if revoke_during_hash else 2)
