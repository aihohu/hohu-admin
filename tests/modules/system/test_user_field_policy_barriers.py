"""Real committed G/B races in disposable schemas, never shared business rows."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.exceptions import BusinessException
from app.core.tenant import TenantContext
from app.db.session import engine
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyAudit,
    UserFieldPolicyBinding,
    UserFieldPolicyRevision,
    UserFieldPolicyState,
)
from app.modules.system.service.field_policy_barrier_service import (
    field_policy_barrier_service as barriers,
)
from app.modules.system.service.user_field_policy_service import (
    user_field_policy_service as policies,
)
from tests.modules.system.field_policy_test_support import isolated_field_policy_schema


@pytest.fixture
async def policy_sessions():
    base = create_async_engine(engine.url, poolclass=NullPool)
    try:
        async with isolated_field_policy_schema(base) as scoped:
            factory = async_sessionmaker(
                scoped, expire_on_commit=False, autoflush=False
            )
            async with factory() as db:
                for tenant_id in (0, 1):
                    await db.execute(
                        insert(Tenant).values(
                            tenant_id=tenant_id,
                            tenant_code=f"test-{tenant_id}",
                            tenant_name=f"Test {tenant_id}",
                            status="1",
                            lifecycle_state="active",
                            bootstrap_version=1,
                            row_version=1,
                            bootstrap_key_hash="a" * 64 if tenant_id else None,
                            bootstrap_fingerprint="b" * 64 if tenant_id else None,
                        )
                    )
                    await db.execute(
                        insert(User).values(
                            tenant_id=tenant_id,
                            user_id=101 + tenant_id,
                            user_name="reviewer",
                            hashed_password="not-a-credential",
                            status="1",
                        )
                    )
                    await db.execute(
                        insert(Role).values(
                            tenant_id=tenant_id,
                            role_id=201 + tenant_id,
                            role_name="candidate",
                            role_code="CANDIDATE",
                            status="1",
                            data_scope="1",
                        )
                    )
                await db.commit()
            contexts = tuple(
                TenantContext(i, f"test-{i}", 101 + i, 1, "access_token")
                for i in (0, 1)
            )
            yield factory, contexts
    finally:
        await base.dispose()


async def assert_unavailable(awaitable):
    with pytest.raises(BusinessException) as error:
        await awaitable
    assert error.value.error_code == "FIELD_POLICY_UNAVAILABLE"
    assert error.value.code == 503


@pytest.mark.parametrize("missing", ["global", "tenant"])
async def test_missing_barrier_fails_closed_without_repair(policy_sessions, missing):
    factory, contexts = policy_sessions
    # Static statements affect only this fixture's disposable schema. TRUNCATE
    # simulates an absent row without weakening the product's DELETE protection.
    truncate = {
        "global": "TRUNCATE ONLY sys_user_field_policy_global_guard",
        "tenant": "TRUNCATE ONLY sys_user_field_policy_state",
    }[missing]
    count = {
        "global": "SELECT count(*) FROM sys_user_field_policy_global_guard",
        "tenant": "SELECT count(*) FROM sys_user_field_policy_state",
    }[missing]
    async with factory() as setup:
        await setup.execute(text(truncate))
        await setup.commit()
    async with factory() as db:
        await assert_unavailable(barriers.acquire(db, tenants=(contexts[0],)))
        assert await db.scalar(text(count)) == 0


async def test_exclusive_global_blocks_other_tenant_until_commit(policy_sessions):
    factory, contexts = policy_sessions
    async with factory() as holder, factory() as contender:
        await barriers.acquire(holder, tenants=(), global_mode="exclusive")
        await assert_unavailable(
            barriers.acquire(
                contender,
                tenants=(contexts[1],),
                mode="exclusive",
                timeout_ms=80,
            )
        )
        await contender.rollback()
        await holder.commit()
        await barriers.acquire(contender, tenants=(contexts[1],), mode="exclusive")
        await contender.commit()


async def test_tenant_exclusive_serializes_only_same_tenant(policy_sessions):
    factory, contexts = policy_sessions
    async with factory() as first, factory() as other, factory() as same:
        await barriers.acquire(first, tenants=(contexts[0],), mode="exclusive")
        await barriers.acquire(
            other, tenants=(contexts[1],), mode="exclusive", timeout_ms=80
        )
        await assert_unavailable(
            barriers.acquire(same, tenants=(contexts[0],), timeout_ms=80)
        )
        await same.rollback()
        await first.rollback()
        await barriers.acquire(same, tenants=(contexts[0],), mode="exclusive")


async def test_shared_tenant_readers_coexist_and_block_writer(policy_sessions):
    factory, contexts = policy_sessions
    async with factory() as first, factory() as second, factory() as writer:
        await barriers.acquire(first, tenants=(contexts[0],))
        await barriers.acquire(second, tenants=(contexts[0],), timeout_ms=80)
        await assert_unavailable(
            barriers.acquire(
                writer,
                tenants=(contexts[0],),
                mode="exclusive",
                timeout_ms=80,
            )
        )


async def test_global_writer_waits_for_shared_global_even_for_other_tenant(
    policy_sessions,
):
    factory, contexts = policy_sessions
    async with factory() as reader, factory() as writer:
        await barriers.acquire(reader, tenants=(contexts[0],))
        await assert_unavailable(
            barriers.acquire(
                writer,
                tenants=(contexts[1],),
                global_mode="exclusive",
                timeout_ms=80,
            )
        )


@pytest.mark.parametrize("upgrade", ["global", "tenant"])
async def test_shared_lock_upgrade_is_rejected_without_poisoning_transaction(
    policy_sessions, upgrade
):
    factory, contexts = policy_sessions
    async with factory() as db:
        await barriers.acquire(db, tenants=(contexts[0],))
        kwargs = (
            {"global_mode": "exclusive"}
            if upgrade == "global"
            else {"mode": "exclusive"}
        )
        with pytest.raises(BusinessException) as error:
            await barriers.acquire(db, tenants=(contexts[0],), **kwargs)
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        assert await db.scalar(text("SELECT 1")) == 1


@pytest.mark.parametrize("first,second", [(0, 1), (1, 0)])
async def test_existing_tenant_locks_cannot_be_extended_later(
    policy_sessions, first, second
):
    factory, contexts = policy_sessions
    async with factory() as db:
        await barriers.acquire(db, tenants=(contexts[first],))
        with pytest.raises(BusinessException) as error:
            await barriers.acquire(db, tenants=(contexts[second],))
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"


async def test_initial_complete_lock_set_can_follow_global_only_lock(policy_sessions):
    factory, contexts = policy_sessions
    async with factory() as db:
        await barriers.acquire(db, tenants=())
        await barriers.acquire(db, tenants=contexts, mode="exclusive")
        await barriers.acquire(db, tenants=(contexts[0],))
        await barriers.acquire(db, tenants=contexts, mode="exclusive")


async def test_unsorted_tenant_set_is_rejected(policy_sessions):
    factory, contexts = policy_sessions
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await barriers.acquire(db, tenants=tuple(reversed(contexts)))
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"


async def test_success_preserves_existing_local_lock_timeout(policy_sessions):
    factory, contexts = policy_sessions
    async with factory() as db:
        await db.execute(text("SET LOCAL lock_timeout = '750ms'"))
        await barriers.acquire(db, tenants=(contexts[0],), timeout_ms=80)
        assert await db.scalar(text("SHOW lock_timeout")) == "750ms"


@pytest.mark.parametrize("ending", ["commit", "rollback"])
async def test_finished_outer_transaction_cannot_reuse_old_lock_proof(
    policy_sessions, ending
):
    factory, contexts = policy_sessions
    async with factory() as reused, factory() as holder:
        await barriers.acquire(reused, tenants=(contexts[0],))
        await getattr(reused, ending)()
        await barriers.acquire(holder, tenants=(contexts[0],), mode="exclusive")
        await assert_unavailable(
            barriers.acquire(reused, tenants=(contexts[0],), timeout_ms=80)
        )
        await reused.rollback()
        await holder.rollback()
        await barriers.acquire(reused, tenants=(contexts[0],), mode="exclusive")


async def test_rejected_nested_acquisition_does_not_release_outer_locks(
    policy_sessions,
):
    factory, contexts = policy_sessions
    async with factory() as holder, factory() as contender:
        await barriers.acquire(holder, tenants=(contexts[0],))
        savepoint = await holder.begin_nested()
        with pytest.raises(BusinessException) as error:
            await barriers.acquire(holder, tenants=(contexts[0],))
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        await savepoint.rollback()
        await barriers.acquire(holder, tenants=(contexts[0],))
        await assert_unavailable(
            barriers.acquire(
                contender,
                tenants=(contexts[0],),
                mode="exclusive",
                timeout_ms=80,
            )
        )
        await contender.rollback()
        await holder.rollback()
        await barriers.acquire(contender, tenants=(contexts[0],), mode="exclusive")


async def test_first_acquisition_inside_savepoint_never_records_proof(policy_sessions):
    factory, contexts = policy_sessions
    async with factory() as db:
        savepoint = await db.begin_nested()
        with pytest.raises(BusinessException) as error:
            await barriers.acquire(db, tenants=(contexts[0],))
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        await savepoint.rollback()
        await barriers.acquire(db, tenants=(contexts[0],), mode="exclusive")


@pytest.mark.parametrize("blocked_on", ["global", "tenant"])
async def test_cancelled_wait_preserves_cancellation_and_rollback_allows_reuse(
    policy_sessions, blocked_on
):
    factory, contexts = policy_sessions
    async with factory() as holder, factory() as waiting:
        if blocked_on == "global":
            await barriers.acquire(holder, tenants=(), global_mode="exclusive")
        else:
            await barriers.acquire(holder, tenants=(contexts[0],), mode="exclusive")
        task = asyncio.create_task(barriers.acquire(waiting, tenants=(contexts[0],)))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await waiting.rollback()
            await holder.rollback()
            await barriers.acquire(waiting, tenants=(contexts[0],), mode="exclusive")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _publish(db, tenant, *, correlation):
    return await policies.publish(
        db,
        tenant=tenant,
        role_id=201,
        grants=(FieldGrant("201", "nickname", "read", "management.read"),),
        expected_version=0,
        reason="reviewed candidate",
        correlation_id=correlation,
    )


async def _seed_second_role(factory):
    async with factory() as db:
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=203,
                role_name="second",
                role_code="SECOND",
                status="1",
                data_scope="1",
            )
        )
        await db.commit()


async def _publish_second_role(db, tenant):
    return await policies.publish(
        db,
        tenant=tenant,
        role_id=203,
        grants=(FieldGrant("203", "nickname", "read", "management.read"),),
        expected_version=0,
        reason="second candidate",
        correlation_id="second-role",
    )


async def test_candidate_write_cannot_take_new_role_lock_after_actor_lock(
    policy_sessions,
):
    factory, contexts = policy_sessions
    await _seed_second_role(factory)
    async with factory() as db:
        await _publish(db, contexts[0], correlation="first-role")
        with pytest.raises(BusinessException) as error:
            await _publish_second_role(db, contexts[0])
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        assert (
            await db.scalar(select(func.count()).select_from(UserFieldPolicyRevision))
            == 1
        )


async def test_ready_actor_lock_cannot_be_followed_by_new_role_lock(policy_sessions):
    factory, contexts = policy_sessions
    await _seed_second_role(factory)
    async with factory() as setup:
        await _publish(setup, contexts[0], correlation="initial-candidate")
        state = await policies.get_state(setup, tenant=contexts[0])
        await setup.commit()
    async with factory() as db:
        await policies.mark_ready(
            db,
            tenant=contexts[0],
            expected_epoch=state.epoch,
            expected_manifest_digest=state.candidate_manifest_digest,
            reason="reviewed",
            correlation_id="ready-first",
        )
        with pytest.raises(BusinessException) as error:
            await _publish_second_role(db, contexts[0])
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"


async def test_same_role_write_then_ready_and_republish_remains_valid(policy_sessions):
    factory, contexts = policy_sessions
    async with factory() as db:
        await _publish(db, contexts[0], correlation="same-role")
        state = await policies.get_state(db, tenant=contexts[0])
        await policies.mark_ready(
            db,
            tenant=contexts[0],
            expected_epoch=state.epoch,
            expected_manifest_digest=state.candidate_manifest_digest,
            reason="reviewed",
            correlation_id="ready-after-role",
        )
        result = await policies.publish(
            db,
            tenant=contexts[0],
            role_id=201,
            grants=(FieldGrant("201", "nickname", "read", "management.read"),),
            expected_version=1,
            reason="revised",
            correlation_id="same-role-again",
        )
        assert result.version == 2


@pytest.mark.parametrize("commit", [True, False])
async def test_write_target_does_not_survive_outer_transaction(policy_sessions, commit):
    factory, contexts = policy_sessions
    await _seed_second_role(factory)
    async with factory() as db:
        await _publish(db, contexts[0], correlation="first-transaction")
        await (db.commit() if commit else db.rollback())
        result = await _publish_second_role(db, contexts[0])
        assert result.role_id == "203" and result.version == 1


async def test_failed_audit_savepoint_preserves_outer_write_target(
    policy_sessions, monkeypatch
):
    factory, contexts = policy_sessions
    await _seed_second_role(factory)
    async with factory() as db:
        with monkeypatch.context() as patch:
            patch.setattr(
                policies,
                "_append_audit",
                AsyncMock(side_effect=RuntimeError("audit unavailable")),
            )
            with pytest.raises(RuntimeError, match="audit unavailable"):
                await _publish(db, contexts[0], correlation="failed-audit")
        assert (
            await db.scalar(select(func.count()).select_from(UserFieldPolicyRevision))
            == 0
        )
        with pytest.raises(BusinessException) as error:
            await _publish_second_role(db, contexts[0])
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        result = await _publish(db, contexts[0], correlation="same-target-retry")
        assert result.version == 1


@pytest.mark.parametrize("first_commit", [True, False])
async def test_two_connection_publish_cas_observes_real_commit_or_rollback(
    policy_sessions, first_commit
):
    factory, contexts = policy_sessions
    first_written, release_first, second_started = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def first_writer():
        async with factory() as db:
            await _publish(db, contexts[0], correlation="first")
            first_written.set()
            await release_first.wait()
            await (db.commit() if first_commit else db.rollback())

    async def second_writer():
        await first_written.wait()
        async with factory() as db:
            second_started.set()
            try:
                result = await _publish(db, contexts[0], correlation="second")
                await db.commit()
                return result.version
            except BusinessException as exc:
                await db.rollback()
                return exc.error_code

    first = asyncio.create_task(first_writer())
    second = asyncio.create_task(second_writer())
    try:
        await asyncio.wait_for(first_written.wait(), timeout=5)
        await asyncio.wait_for(second_started.wait(), timeout=5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), timeout=0.1)
        release_first.set()
        _, result = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
        assert result == ("FIELD_POLICY_VERSION_CONFLICT" if first_commit else 1)
    finally:
        release_first.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
    async with factory() as db:
        for model in (
            UserFieldPolicyRevision,
            UserFieldPolicyBinding,
            UserFieldPolicyAudit,
        ):
            assert await db.scalar(select(func.count()).select_from(model)) == 1
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 2
        )
        assert (
            await db.scalar(
                select(UserFieldPolicyState.mode).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == "legacy"
        )
