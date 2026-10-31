"""Fixed-key DML and aggregate epochs against committed disposable schemas."""

from contextlib import contextmanager

import pytest
from sqlalchemy import insert, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    NewResource,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.db.base import user_roles
from app.db.session import engine
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from tests.committed_database import isolated_migrated_schema

USER = ResourceKey(ResourceKind.USER, 0, 101)
ROLE = ResourceKey(ResourceKind.ROLE, 0, 201)
NEW = ResourceKey(ResourceKind.USER, 0, 102)
LINK = ResourceKey(ResourceKind.USER_ROLES, 0, (101, 201))


@contextmanager
def rejected(code="FIELD_POLICY_LOCK_ORDER"):
    with pytest.raises(BusinessException) as caught:
        yield
    assert caught.value.error_code == code


@pytest.fixture
async def mutation_sessions():
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
                    user_name="mutation",
                    nickname="before",
                    hashed_password="fixture",
                    status="1",
                )
            )
            await db.execute(
                insert(Role).values(
                    tenant_id=0,
                    role_id=201,
                    role_name="mutation",
                    role_code="MUTATION",
                    status="1",
                )
            )
            await db.execute(
                update(UserFieldPolicyState)
                .where(UserFieldPolicyState.tenant_id == 0)
                .values(mode="ready", epoch=2, candidate_manifest_digest="a" * 64)
            )
            await db.commit()
        yield managed, plain


def scope(db, mode=LockMode.EXCLUSIVE):
    return transactions.scope(db, tenants=(TenantLock(0, mode),))


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


async def test_real_mutations_bump_once_and_preserve_candidate(mutation_sessions):
    managed, plain = mutation_sessions
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(ROLE, USER)))
            async with tx.aggregate(0):
                assert await tx.update_held(USER, {"nickname": "after"})
                assert await tx.update_held(ROLE, {"role_desc": "after"})
            assert await state(db) == (3, "legacy", "a" * 64)
        await db.commit()
    async with plain() as db:
        assert await state(db) == (3, "legacy", "a" * 64)
        assert (
            await db.scalar(select(User.nickname).where(User.user_id == 101)) == "after"
        )


async def test_noop_does_not_advance_epoch_or_invalidate_ready(mutation_sessions):
    managed, _ = mutation_sessions
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(USER,)))
            async with tx.aggregate(0):
                assert not await tx.update_held(
                    USER, {"nickname": "before"}, expected={"auth_version": 1}
                )
                assert not await tx.update_held(USER, {})
            assert await state(db) == (2, "ready", "a" * 64)
        await db.commit()


async def test_expected_is_checked_before_noop(mutation_sessions):
    managed, plain = mutation_sessions
    async with managed() as db:
        with rejected("AUTHORIZATION_SNAPSHOT_STALE"):
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                async with tx.aggregate(0):
                    await tx.update_held(
                        USER, {"nickname": "before"}, expected={"auth_version": 2}
                    )
        with rejected():
            await db.commit()
        await db.rollback()
    async with plain() as db:
        assert await state(db) == (2, "ready", "a" * 64)


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": 999},
        {"tenant_id": 1},
        {"create_time": None},
        {"unknown": 1},
        {"nickname": select(literal("sql")).scalar_subquery()},
        {"nickname": {"nested": [literal("sql")]}},
    ],
)
async def test_unapproved_columns_and_sql_values_are_fatal(mutation_sessions, changes):
    managed, _ = mutation_sessions
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                async with tx.aggregate(0):
                    await tx.update_held(USER, changes)
        with rejected():
            await db.commit()
        await db.rollback()


@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_mutation_without_active_aggregate_is_rejected(
    mutation_sessions, operation
):
    managed, _ = mutation_sessions
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                if operation == "update":
                    await tx.update_held(USER, {"nickname": "outside"})
                else:
                    await tx.delete_held(USER)
        await db.rollback()


@pytest.mark.parametrize("mode,tenant", [(LockMode.SHARED, 0), (LockMode.EXCLUSIVE, 1)])
async def test_aggregate_requires_its_complete_exclusive_tenant(
    mutation_sessions, mode, tenant
):
    managed, _ = mutation_sessions
    async with managed() as db:
        with rejected():
            async with scope(db, mode) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                async with tx.aggregate(tenant):
                    pass
        await db.rollback()


async def test_unheld_key_cannot_be_updated(mutation_sessions):
    managed, _ = mutation_sessions
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan())
                async with tx.aggregate(0):
                    await tx.update_held(USER, {"nickname": "unheld"})
        await db.rollback()


async def test_nested_aggregate_is_rejected(mutation_sessions):
    managed, _ = mutation_sessions
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan())
                async with tx.aggregate(0):
                    async with tx.aggregate(0):
                        pass
        await db.rollback()


async def test_separate_aggregates_in_same_outer_advance_separately(mutation_sessions):
    managed, _ = mutation_sessions
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(USER,)))
            for name in ("first", "second"):
                async with tx.aggregate(0):
                    assert await tx.update_held(USER, {"nickname": name})
            assert await state(db) == (4, "legacy", "a" * 64)
        await db.rollback()


@pytest.mark.parametrize("partial", [False, True])
async def test_chunk_rollback_rewinds_released_row_mutation_ledger(
    mutation_sessions, partial
):
    managed, plain = mutation_sessions
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(USER, ROLE)))
            async with tx.aggregate(0):
                with pytest.raises(ValueError):
                    async with tx.savepoint():
                        async with tx.savepoint():
                            await tx.update_held(USER, {"nickname": "rolled-back"})
                        raise ValueError("rollback chunk")
                if partial:
                    await tx.update_held(ROLE, {"role_desc": "survives"})
            assert await state(db) == (
                3 if partial else 2,
                "legacy" if partial else "ready",
                "a" * 64,
            )
        await db.commit()
    async with plain() as db:
        assert (
            await db.scalar(select(User.nickname).where(User.user_id == 101))
            == "before"
        )


async def test_aggregate_inside_rolled_back_savepoint_does_not_leave_epoch(
    mutation_sessions,
):
    managed, _ = mutation_sessions
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(USER,)))
            with pytest.raises(ValueError):
                async with tx.savepoint():
                    async with tx.aggregate(0):
                        await tx.update_held(USER, {"nickname": "rolled-back"})
                    assert await state(db) == (3, "legacy", "a" * 64)
                    raise ValueError("rollback aggregate")
            assert await state(db) == (2, "ready", "a" * 64)
            async with tx.aggregate(0):
                await tx.update_held(USER, {"nickname": "survives"})
            assert await state(db) == (3, "legacy", "a" * 64)
        await db.commit()


async def test_insert_records_real_change_in_aggregate(mutation_sessions):
    managed, _ = mutation_sessions
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(new=(NewResource(NEW),)))
            async with tx.aggregate(0):
                await tx.insert_reserved(
                    NEW,
                    {"user_name": "new", "hashed_password": "fixture", "status": "1"},
                )
            assert await state(db) == (3, "legacy", "a" * 64)
        await db.commit()


async def test_explicit_junction_delete_then_parent_delete_has_no_hidden_cascade(
    mutation_sessions,
):
    managed, plain = mutation_sessions
    async with plain() as db:
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=101, role_id=201)
        )
        await db.commit()
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(ROLE, USER, LINK)))
            async with tx.aggregate(0):
                assert await tx.delete_held(LINK)
                with rejected():
                    tx.assert_held(LINK)
                assert await tx.delete_held(ROLE, expected={"role_code": "MUTATION"})
            assert await state(db) == (3, "legacy", "a" * 64)
        await db.commit()


async def test_parent_delete_refuses_unplanned_cascade(mutation_sessions):
    managed, plain = mutation_sessions
    async with plain() as db:
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=101, role_id=201)
        )
        await db.commit()
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(ROLE,)))
                async with tx.aggregate(0):
                    await tx.delete_held(ROLE)
        await db.rollback()
    async with plain() as db:
        assert await db.scalar(select(user_roles.c.role_id)) == 201
        assert await state(db) == (2, "ready", "a" * 64)


async def test_savepoint_rollback_restores_deleted_key_proof(mutation_sessions):
    managed, _ = mutation_sessions
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(ROLE,)))
            async with tx.aggregate(0):
                with pytest.raises(ValueError):
                    async with tx.savepoint():
                        await tx.delete_held(ROLE)
                        with rejected():
                            tx.assert_held(ROLE)
                        raise ValueError("restore deletion")
                tx.assert_held(ROLE)
            assert await state(db) == (2, "ready", "a" * 64)
        await db.commit()


async def test_update_unique_conflict_is_recoverable_only_in_controlled_savepoint(
    mutation_sessions,
):
    managed, plain = mutation_sessions
    async with plain() as db:
        await db.execute(
            insert(User).values(
                tenant_id=0,
                user_id=102,
                user_name="second",
                hashed_password="fixture",
                status="1",
            )
        )
        await db.commit()
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(USER, NEW)))
            async with tx.aggregate(0):
                with pytest.raises(IntegrityError):
                    async with tx.savepoint():
                        await tx.update_held(NEW, {"user_name": "mutation"})
                tx.assert_held(USER, NEW)
                await tx.update_held(USER, {"nickname": "survives"})
            assert await state(db) == (3, "legacy", "a" * 64)
        await db.commit()
    async with plain() as db:
        assert (
            await db.scalar(select(User.user_name).where(User.user_id == 102))
            == "second"
        )


async def test_all_failed_insert_rows_do_not_advance_epoch(mutation_sessions):
    managed, _ = mutation_sessions
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(new=(NewResource(NEW),)))
            async with tx.aggregate(0):
                with pytest.raises(IntegrityError):
                    async with tx.savepoint():
                        await tx.insert_reserved(
                            NEW,
                            {
                                "user_name": "mutation",
                                "hashed_password": "fixture",
                                "status": "1",
                            },
                        )
            assert await state(db) == (2, "ready", "a" * 64)
        await db.commit()


async def test_caught_aggregate_exception_cannot_commit_unbumped_mutation(
    mutation_sessions,
):
    managed, plain = mutation_sessions
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                with pytest.raises(ValueError):
                    async with tx.aggregate(0):
                        await tx.update_held(USER, {"nickname": "must-rollback"})
                        raise ValueError("aggregate failure")
        with rejected():
            await db.commit()
        await db.rollback()
    async with plain() as db:
        assert await state(db) == (2, "ready", "a" * 64)
        assert (
            await db.scalar(select(User.nickname).where(User.user_id == 101))
            == "before"
        )


async def test_delete_expected_snapshot_is_checked_before_dml(mutation_sessions):
    managed, plain = mutation_sessions
    async with managed() as db:
        with rejected("AUTHORIZATION_SNAPSHOT_STALE"):
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(ROLE,)))
                async with tx.aggregate(0):
                    await tx.delete_held(ROLE, expected={"status": "2"})
        await db.rollback()
    async with plain() as db:
        assert await db.scalar(select(Role.role_id)) == 201
        assert await state(db) == (2, "ready", "a" * 64)


async def test_user_without_dependent_rows_can_be_explicitly_deleted(mutation_sessions):
    managed, plain = mutation_sessions
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(USER,)))
            async with tx.aggregate(0):
                assert await tx.delete_held(USER)
            with rejected():
                tx.assert_held(USER)
        await db.commit()
    async with plain() as db:
        assert await db.scalar(select(User.user_id)) is None
        assert await state(db) == (3, "legacy", "a" * 64)


@pytest.mark.parametrize("expected", [{"auth_version": literal(1)}, {"unknown": 1}, []])
async def test_expected_cannot_contain_sql_or_unknown_shape(
    mutation_sessions, expected
):
    managed, _ = mutation_sessions
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan(existing=(USER,)))
                async with tx.aggregate(0):
                    await tx.update_held(USER, {}, expected=expected)
        await db.rollback()


@pytest.mark.parametrize("kind", ["aggregate", "policy"])
async def test_scope_cannot_close_with_an_unfinished_mutation_frame(
    mutation_sessions, kind
):
    managed, _ = mutation_sessions
    async with managed() as db:
        frame = None
        try:
            with rejected():
                async with scope(db) as tx:
                    await tx.lock(LockPlan(existing=(USER,)))
                    frame = (
                        tx.aggregate(0)
                        if kind == "aggregate"
                        else tx.policy_change(0, action="ready")
                    )
                    await frame.__aenter__()
                    if kind == "aggregate":
                        await tx.update_held(USER, {"nickname": "unfinished"})
        finally:
            if frame is not None:
                with rejected():
                    await frame.__aexit__(None, None, None)
        with rejected():
            await db.commit()
        await db.rollback()


async def test_role_agent_delete_is_explicit_and_advances_authorization_epoch(
    mutation_sessions,
):
    managed, plain = mutation_sessions
    link = ResourceKey(ResourceKind.ROLE_AGENT, 0, (201, 301))
    async with plain() as db:
        await db.execute(
            insert(AiAgent).values(
                agent_id=301, code="mutation", name="test", description="fixture"
            )
        )
        await db.execute(
            insert(RoleAiAgent).values(tenant_id=0, role_id=201, agent_id=301)
        )
        await db.commit()
    async with managed() as db:
        async with scope(db) as tx:
            await tx.lock(LockPlan(existing=(ROLE, link)))
            async with tx.aggregate(0):
                assert await tx.delete_held(link)
            assert await state(db) == (3, "legacy", "a" * 64)
        await db.commit()
    async with plain() as db:
        assert await db.scalar(select(RoleAiAgent.agent_id)) is None
        assert await db.scalar(select(AiAgent.agent_id)) == 301


@pytest.mark.parametrize("operation", ["insert", "update"])
async def test_role_agent_binding_mutation_remains_outside_this_slice(
    mutation_sessions, operation
):
    managed, plain = mutation_sessions
    link = ResourceKey(ResourceKind.ROLE_AGENT, 0, (201, 301))
    async with plain() as db:
        await db.execute(
            insert(AiAgent).values(
                agent_id=301, code="mutation", name="test", description="fixture"
            )
        )
        if operation == "update":
            await db.execute(
                insert(RoleAiAgent).values(tenant_id=0, role_id=201, agent_id=301)
            )
        await db.commit()
    plan = (
        LockPlan(existing=(ROLE, link))
        if operation == "update"
        else LockPlan(existing=(ROLE,), new=(NewResource(link),))
    )
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(plan)
                async with tx.aggregate(0):
                    if operation == "update":
                        await tx.update_held(link, {"enabled": False})
                    else:
                        await tx.insert_reserved(link, {"enabled": True})
        await db.rollback()


@pytest.mark.parametrize("action", [{}, [], "unknown"])
async def test_policy_frame_rejects_invalid_action_as_domain_error(
    mutation_sessions, action
):
    managed, _ = mutation_sessions
    async with managed() as db:
        with rejected():
            async with scope(db) as tx:
                await tx.lock(LockPlan())
                async with tx.policy_change(0, action=action):
                    pass
        await db.rollback()
