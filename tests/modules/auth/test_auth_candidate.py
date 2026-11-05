"""Explicit Auth candidates never fall back to legacy transaction writers."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import event, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.authorization_plan import LockMode, LockPlan, TenantLock
from app.core.exceptions import BusinessException
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
)
from app.db import authorization_owner
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.db.base import user_roles
from app.db.session import engine
from app.modules.auth import candidate_owner
from app.modules.auth.candidate_owner import PostCommitEffects, auth_candidate_owner
from app.modules.auth.candidate_service import auth_candidate_service
from app.modules.auth.schemas.auth import LoginCredentials
from app.modules.auth.schemas.self_service import (
    EmailBinding,
    EmailCodeRequest,
    Registration,
)
from app.modules.system.models.mail_config import MailConfig
from app.modules.system.models.role import Role
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from tests.committed_database import isolated_migrated_schema


@pytest.mark.asyncio
async def test_candidate_rejects_ordinary_session_before_external_work():
    async with AsyncSession() as db:
        with pytest.raises(BusinessException) as error:
            await auth_candidate_owner.current(db, "untrusted")
    assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"


@pytest.fixture
async def accounts(monkeypatch):
    async with isolated_migrated_schema(engine.url) as scoped:
        plain = async_sessionmaker(scoped, expire_on_commit=False, autoflush=False)
        managed = async_sessionmaker(
            scoped,
            class_=AuthorizationPlanSession,
            expire_on_commit=False,
            autoflush=False,
        )
        async with plain() as db:
            await db.execute(
                insert(User).values(
                    tenant_id=0,
                    user_id=101,
                    user_name="alice",
                    nickname="Alice",
                    hashed_password="encoded:OldStrong42!",
                    user_email="alice@example.com",
                    email_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
                    status="1",
                    auth_version=3,
                )
            )
            await db.execute(
                insert(Role).values(
                    tenant_id=0,
                    role_id=201,
                    role_name="Public",
                    role_code="PUBLIC",
                    status="1",
                )
            )
            for sid, key, value in (
                (301, "register_enabled", "true"),
                (302, "password_reset_enabled", "true"),
                (303, "email_code_minutes", "5"),
                (304, "registration_role_id", "201"),
            ):
                await db.execute(
                    insert(SystemSetting).values(
                        tenant_id=0,
                        setting_id=sid,
                        setting_key=key,
                        setting_value=value,
                        status="1",
                    )
                )
            await db.execute(
                update(MailConfig)
                .where(MailConfig.config_id == 1)
                .values(
                    values={
                        "enabled": True,
                        "host": "mail.example.com",
                        "from_email": "sender@example.com",
                    }
                )
            )
            await db.execute(
                update(UserFieldPolicyState)
                .where(UserFieldPolicyState.tenant_id == 0)
                .values(mode="ready", epoch=2, candidate_manifest_digest="a" * 64)
            )
            version = await db.scalar(
                select(Tenant.row_version).where(Tenant.tenant_id == 0)
            )
            await db.commit()
        consumed, sent, external = [], [], []

        async def hashed(password):
            external.append("hash")
            return "encoded:" + password

        async def verified(password, encoded):
            external.append("verify")
            return encoded == "encoded:" + password

        async def consume(*args):
            consumed.append(args)

        async def blacklist(*_args):
            return False

        async def rotate(*_args):
            consumed.append(("rotate",))
            return True

        async def notify(*args, **_kwargs):
            sent.append(args)

        monkeypatch.setattr(candidate_owner, "hash_password", hashed)
        monkeypatch.setattr(candidate_owner, "verify_password", verified)
        monkeypatch.setattr(candidate_owner.email_verification, "consume", consume)
        monkeypatch.setattr(candidate_owner, "_is_blacklisted", blacklist)
        monkeypatch.setattr(candidate_owner, "_try_blacklist_token", rotate)
        monkeypatch.setattr(auth_candidate_owner, "_notify", notify)
        yield SimpleNamespace(
            plain=plain,
            managed=managed,
            consumed=consumed,
            sent=sent,
            external=external,
            version=version,
        )


def token(accounts, *, refresh=False, uver=3):
    return (create_refresh_token if refresh else create_access_token)(
        subject="101", tenant_id=0, tenant_version=accounts.version, user_version=uver
    )


def registration(name="newuser"):
    return Registration(
        user_name=name,
        email=f"{name}@example.com",
        code="123456",
        password="NewStrong42!",
    )


async def state(db):
    return (
        await db.execute(
            select(
                UserFieldPolicyState.epoch,
                UserFieldPolicyState.mode,
                UserFieldPolicyState.candidate_manifest_digest,
            ).where(UserFieldPolicyState.tenant_id == 0)
        )
    ).one()


async def protected_done(db):
    async with authorization_transactions.scope(db, tenants=(TenantLock(0),)):
        pass


async def test_register_uses_complete_role_user_junction_plan_and_one_epoch(accounts):
    async with accounts.managed() as db:
        uid = int(await auth_candidate_owner.register(db, 0, registration()))
    assert len(accounts.consumed) == 1
    async with accounts.plain() as db:
        assert (
            await db.scalar(select(User.hashed_password).where(User.user_id == uid))
            == "encoded:NewStrong42!"
        )
        assert (
            await db.execute(
                select(user_roles.c.role_id).where(
                    user_roles.c.tenant_id == 0, user_roles.c.user_id == uid
                )
            )
        ).scalar_one() == 201
        assert await state(db) == (3, "legacy", "a" * 64)


@pytest.mark.parametrize("change", ["setting", "role", "mail"])
async def test_consumed_code_is_not_restored_after_policy_revocation(
    accounts, monkeypatch, change
):
    async def consume(*args):
        accounts.consumed.append(args)
        async with accounts.plain() as db:
            if change == "setting":
                statement = (
                    update(SystemSetting)
                    .where(
                        SystemSetting.tenant_id == 0,
                        SystemSetting.setting_key == "register_enabled",
                    )
                    .values(setting_value="false")
                )
            elif change == "role":
                statement = (
                    update(Role)
                    .where(Role.tenant_id == 0, Role.role_id == 201)
                    .values(status="2")
                )
            else:
                statement = (
                    update(MailConfig)
                    .where(MailConfig.config_id == 1)
                    .values(version=2)
                )
            await db.execute(statement)
            await db.commit()

    monkeypatch.setattr(candidate_owner.email_verification, "consume", consume)
    async with accounts.managed() as db:
        with pytest.raises(BusinessException) as error:
            await auth_candidate_owner.register(db, 0, registration())
        assert error.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
    assert len(accounts.consumed) == 1
    async with accounts.plain() as db:
        assert (
            await db.scalar(
                select(User.user_id).where(
                    User.tenant_id == 0, User.user_name == "newuser"
                )
            )
            is None
        )
        assert await state(db) == (2, "ready", "a" * 64)


async def test_reset_advances_security_and_epoch_then_notifies(accounts):
    payload = Registration(
        user_name="alice",
        email="alice@example.com",
        code="123456",
        password="NewStrong42!",
    )
    async with accounts.managed() as db:
        await auth_candidate_owner.reset(db, 0, payload)
    async with accounts.plain() as db:
        assert (
            await db.execute(
                select(User.auth_version, User.hashed_password).where(
                    User.user_id == 101
                )
            )
        ).one() == (4, "encoded:NewStrong42!")
        assert await state(db) == (3, "legacy", "a" * 64)
    assert len(accounts.sent) == 1


async def test_binding_preserves_email_trigger_and_bumps_once(accounts):
    payload = EmailBinding(
        email="new@example.com", password="OldStrong42!", code="123456"
    )
    async with accounts.managed() as db:
        await auth_candidate_owner.bind(db, token(accounts), payload)
    async with accounts.plain() as db:
        row = (
            await db.execute(
                select(User.user_email, User.email_verified_at).where(
                    User.user_id == 101
                )
            )
        ).one()
        assert row.user_email == "new@example.com" and row.email_verified_at is not None
        assert await state(db) == (3, "legacy", "a" * 64)
    assert accounts.sent[0][1] == "alice@example.com"


async def test_binding_rechecks_profile_after_code_consumption(accounts, monkeypatch):
    async def consume(*args):
        accounts.consumed.append(args)
        async with accounts.plain() as db:
            await db.execute(
                update(User)
                .where(User.tenant_id == 0, User.user_id == 101)
                .values(user_email="changed@example.com")
            )
            await db.commit()

    monkeypatch.setattr(candidate_owner.email_verification, "consume", consume)
    async with accounts.managed() as db:
        with pytest.raises(BusinessException) as error:
            await auth_candidate_owner.bind(
                db,
                token(accounts),
                EmailBinding(
                    email="new@example.com", password="OldStrong42!", code="123456"
                ),
            )
        assert error.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
    assert len(accounts.consumed) == 1 and not accounts.sent


async def test_login_current_and_refresh_use_independent_security_versions(accounts):
    async with accounts.managed() as db:
        access, refresh = await auth_candidate_owner.login(
            db, LoginCredentials(user_name="alice", password="OldStrong42!")
        )
        assert decode_access_token(access)["uver"] == "3"
        identity = await auth_candidate_owner.current(db, access)
        assert (
            identity.user_version == 3 and identity.tenant_version == accounts.version
        )
        new_access, _ = await auth_candidate_owner.refresh(db, refresh)
        assert decode_access_token(new_access)["uver"] == "3"
    async with accounts.plain() as db:
        assert await state(db) == (2, "ready", "a" * 64)


async def test_refresh_rotation_precedes_current_db_revalidation(accounts):
    async with accounts.managed() as db:
        with pytest.raises(BusinessException) as error:
            await auth_candidate_owner.refresh(
                db, token(accounts, refresh=True, uver=2)
            )
        assert error.value.error_code == "TOKEN_EXPIRED"
    assert accounts.consumed == [("rotate",)]


async def test_hash_and_proof_run_with_no_transaction_or_barriers(
    accounts, monkeypatch
):
    async with accounts.managed() as db:

        async def hashed(password):
            assert not db.in_transaction()
            assert db._authorization_journal.phase == "idle"
            # A separate writer can enter G/B while expensive work is waiting.
            async with accounts.managed() as other:
                async with authorization_transactions.scope(
                    other, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
                ):
                    pass
                await other.rollback()
            return "encoded:" + password

        async def consume(*args):
            assert not db.in_transaction()
            accounts.consumed.append(args)

        monkeypatch.setattr(candidate_owner, "hash_password", hashed)
        monkeypatch.setattr(candidate_owner.email_verification, "consume", consume)
        await auth_candidate_owner.register(db, 0, registration())


async def test_prepare_email_releases_barriers_before_redis_and_smtp(
    accounts, monkeypatch
):
    async with accounts.managed() as db:

        async def throttle(*_args):
            assert not db.in_transaction()

        async def issue(*_args):
            assert not db.in_transaction()
            return "123456"

        monkeypatch.setattr(candidate_owner.email_verification, "throttle", throttle)
        monkeypatch.setattr(candidate_owner.email_verification, "issue", issue)
        result = await auth_candidate_owner.prepare_email(
            db,
            0,
            EmailCodeRequest(
                user_name="newuser", email="newuser@example.com", purpose="register"
            ),
            ip="127.0.0.1",
        )
    assert result == {"expiresIn": 300, "retryAfter": 60}
    assert len(accounts.sent) == 1


async def test_commit_unknown_discards_effects_and_quarantines_session(
    accounts, monkeypatch
):
    effects, calls = PostCommitEffects(), []

    async def effect():
        calls.append("cache")

    effects.cache("key", effect)
    async with accounts.managed() as db:
        await protected_done(db)

        async def unknown():
            raise OSError("lost acknowledgement")

        monkeypatch.setattr(db, "commit", unknown)
        with pytest.raises(BusinessException) as error:
            await auth_candidate_owner._commit(db, effects)
        assert error.value.error_code == "AUTHORIZATION_COMMIT_UNKNOWN"
        with pytest.raises(BusinessException):
            await auth_candidate_owner.current(db, token(accounts))
    await effects.confirmed()
    assert not calls


async def test_cache_effects_coalesce_and_are_bounded():
    effects, calls = PostCommitEffects(), []

    async def first():
        calls.append("first")

    async def last():
        calls.append("last")

    effects.cache("key", first)
    effects.cache("key", last)
    await effects.confirmed()
    await effects.confirmed()
    assert calls == ["last"]
    for index in range(256):
        effects.cache(str(index), last)
    with pytest.raises(BusinessException):
        effects.cache("overflow", last)


async def test_commit_cancellation_remains_cancellation(accounts, monkeypatch):
    async with accounts.managed() as db:
        await protected_done(db)

        async def cancelled():
            raise asyncio.CancelledError

        monkeypatch.setattr(db, "commit", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await auth_candidate_owner._commit(db, PostCommitEffects())


async def test_unknown_commit_invalidates_real_connection(accounts, monkeypatch):
    async with accounts.managed() as db:
        await db.scalar(select(User.user_id).where(User.user_id == 101))
        await protected_done(db)
        connection = db._authorization_journal.connection
        invalidations = []

        def invalidated(*args):
            invalidations.append(args)

        event.listen(connection.engine.pool, "invalidate", invalidated)

        async def unknown():
            raise OSError("lost acknowledgement")

        monkeypatch.setattr(db, "commit", unknown)
        with pytest.raises(BusinessException):
            await auth_candidate_owner._commit(db, PostCommitEffects())
        assert invalidations and connection.closed


@pytest.mark.parametrize("operation", ["commit", "rollback"])
async def test_cancel_resistant_operation_stays_quarantined_until_real_invalidation(
    accounts, monkeypatch, operation
):
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(authorization_owner, "_COMMIT_SECONDS", 0.01)
    monkeypatch.setattr(authorization_owner, "_CLEANUP_SECONDS", 0.01)
    async with accounts.managed() as db:
        await db.scalar(select(User.user_id).where(User.user_id == 101))
        await protected_done(db)
        connection = db._authorization_journal.connection
        invalidations = []

        def invalidated(*args):
            invalidations.append(args)

        event.listen(connection.engine.pool, "invalidate", invalidated)

        async def pending_operation():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        monkeypatch.setattr(db, operation, pending_operation)
        complete = (
            authorization_owner.commit_authorization_session
            if operation == "commit"
            else authorization_owner.rollback_authorization_session
        )
        with pytest.raises(BusinessException) as error:
            await complete(db)
        assert error.value.error_code == f"AUTHORIZATION_{operation.upper()}_UNKNOWN"
        assert entered.is_set()
        assert not invalidations and not connection.closed
        await db.close()
        assert not connection.closed
        with pytest.raises(BusinessException):
            await db.scalar(select(User.user_id))
        cleanup = authorization_owner._QUARANTINED[id(db)][2]
        release.set()
        await asyncio.wait_for(asyncio.shield(cleanup), 1)
        assert invalidations and connection.closed


async def test_external_cancellation_during_commit_is_preserved(accounts, monkeypatch):
    entered = asyncio.Event()
    async with accounts.managed() as db:
        await db.scalar(select(User.user_id).where(User.user_id == 101))
        await protected_done(db)

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(db, "commit", blocked)
        request = asyncio.create_task(
            authorization_owner.commit_authorization_session(db)
        )
        await entered.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert db.info[authorization_owner.QUARANTINE_KEY]


async def test_login_rechecks_password_proof_after_security_change(
    accounts, monkeypatch
):
    async def verify(_password, _encoded):
        async with accounts.plain() as db:
            await db.execute(
                update(User)
                .where(User.tenant_id == 0, User.user_id == 101)
                .values(auth_version=4)
            )
            await db.commit()
        return True

    monkeypatch.setattr(candidate_owner, "verify_password", verify)
    async with accounts.managed() as db:
        with pytest.raises(BusinessException) as error:
            await auth_candidate_owner.login(
                db, LoginCredentials(user_name="alice", password="OldStrong42!")
            )
        assert error.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"


async def test_protected_service_requires_real_same_session_proof(accounts):
    async with accounts.managed() as db, accounts.managed() as other:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            await tx.lock(LockPlan())
            with pytest.raises(BusinessException) as error:
                await auth_candidate_service.apply(other, tx, None, "current")
            assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        await db.rollback()


async def test_commit_ack_budget_starts_after_closed_protected_phase(accounts):
    async with accounts.managed() as db:
        await protected_done(db)
        db._authorization_journal.deadline = asyncio.get_running_loop().time() - 1
        await authorization_owner.commit_authorization_session(db)
        assert not db.in_transaction()
        assert not db.info.get(authorization_owner.QUARANTINE_KEY)


async def test_commit_owner_rejects_idle_session_without_transaction_proof(accounts):
    async with accounts.managed() as db:
        with pytest.raises(BusinessException) as error:
            await authorization_owner.commit_authorization_session(db)
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"


async def test_preparation_cancel_survives_failed_cleanup(monkeypatch):
    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    async def unknown(*_args, **_kwargs):
        raise BusinessException(
            503, "cleanup unknown", error_code="AUTHORIZATION_ROLLBACK_UNKNOWN"
        )

    monkeypatch.setattr(candidate_owner.auth_candidate_accounts, "read", cancelled)
    monkeypatch.setattr(candidate_owner, "rollback_authorization_session", unknown)
    async with AuthorizationPlanSession() as db:
        with pytest.raises(asyncio.CancelledError):
            await auth_candidate_owner._read(db, 0)
