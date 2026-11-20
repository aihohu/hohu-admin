"""AI factual writes use complete proofs without changing policy epochs."""

import pytest
from sqlalchemy import insert, select

from app.core.authorization_facts import FACT_COLUMNS
from app.core.authorization_file import read_local
from app.core.authorization_plan import (
    LockPlan,
    NewResource,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.exceptions import BusinessException
from app.db.authorization_owner import commit_authorization_session
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service.authorization_resource_catalog import resource_spec
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from tests.modules.system.test_authorization_system_candidate import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)

C = ResourceKey(ResourceKind.CONVERSATION, 0, 301)
M = ResourceKey(ResourceKind.SOURCE_MESSAGE, 0, 401)


@pytest.fixture
async def ai_sessions(system_sessions):
    factory, plain = system_sessions
    async with plain() as db:
        await db.execute(
            insert(AiConversation).values(
                tenant_id=0, conversation_id=301, user_id=101, title="before"
            )
        )
        await db.commit()
    return factory, plain


async def test_shared_factual_update_leaves_epoch_unchanged(ai_sessions):
    factory, plain = ai_sessions
    async with plain() as observer:
        initial_epoch = await observer.scalar(
            select(UserFieldPolicyState.epoch).where(
                UserFieldPolicyState.tenant_id == 0
            )
        )
    async with factory() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            await tx.lock(LockPlan(existing=(C,)))
            assert await tx.update_factual(
                C, {"title": "after"}, expected={"title": "before"}
            )
            assert not await tx.update_factual(
                C, {"title": "after"}, expected={"title": "after"}
            )
        await commit_authorization_session(db)
    async with plain() as db:
        assert await db.scalar(select(AiConversation.title)) == "after"
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == initial_epoch
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"content": "changed"},
        {"routing_feedback": "correct", "content": "changed"},
        {"routing_feedback": "invalid"},
    ],
)
async def test_tool_feedback_exception_does_not_allow_other_facts(ai_sessions, changes):
    factory, plain = ai_sessions
    key = ResourceKey(ResourceKind.ASSISTANT_MESSAGE, 0, 405)
    async with plain() as db, db.begin():
        await db.execute(
            insert(AiMessage).values(
                tenant_id=0,
                message_id=405,
                conversation_id=301,
                role="tool",
                content="original",
            )
        )
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                await tx.lock(LockPlan(existing=(C, key)))
                await tx.update_factual(key, changes, expected={"content": "original"})


@pytest.mark.parametrize("file_phase", [False, True])
async def test_sealed_unused_reservation_allows_file_but_never_later_insert(
    ai_sessions, file_phase
):
    factory, _ = ai_sessions
    reached_insert = False
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                await tx.lock(LockPlan(existing=(C,), new=(NewResource(M, parent=C),)))
                tx.seal_insertions()
                if file_phase:
                    with tx.file_io():
                        pass
                reached_insert = True
                await tx.insert_reserved(
                    M, {"conversation_id": 301, "role": "user", "content": "late"}
                )
    assert reached_insert


async def test_insertions_cannot_be_sealed_inside_savepoint(ai_sessions):
    factory, _ = ai_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                await tx.lock(LockPlan(existing=(C,)))
                async with tx.savepoint():
                    tx.seal_insertions()


@pytest.mark.parametrize("changes", [{"user_id": 102}, {"tenant_id": 1}])
async def test_factual_write_cannot_rebind_ownership(ai_sessions, changes):
    factory, _ = ai_sessions
    async with factory() as db:
        with pytest.raises(BusinessException, match="证明"):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                await tx.lock(LockPlan(existing=(C,)))
                await tx.update_factual(C, changes, expected={"title": "before"})


async def test_new_source_unique_scope_is_exact_locked_conversation(ai_sessions):
    factory, plain = ai_sessions
    async with factory() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            await tx.lock(LockPlan(existing=(C,), new=(NewResource(M, parent=C),)))
            await tx.insert_reserved(
                M, {"conversation_id": 301, "role": "user", "content": "source"}
            )
        await commit_authorization_session(db)
    async with plain() as db:
        assert (
            await db.scalar(
                select(AiMessage.content).where(AiMessage.message_id == 401)
            )
            == "source"
        )


async def test_context_parent_mismatch_fails_before_insert(ai_sessions):
    factory, _ = ai_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                await tx.lock(LockPlan(existing=(C,), new=(NewResource(M, parent=C),)))
                await tx.insert_reserved(M, {"conversation_id": 999, "role": "user"})


async def test_file_phase_rejects_uninserted_reservation(ai_sessions):
    factory, _ = ai_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                await tx.lock(LockPlan(existing=(C,), new=(NewResource(M, parent=C),)))
                tx.begin_file_phase()


def test_factual_column_catalog_contains_only_real_columns():
    for kind, columns in FACT_COLUMNS.items():
        spec = resource_spec(kind)
        assert columns <= set(spec.table.c.keys())
        assert not columns.intersection({"tenant_id", *spec.key_columns})


async def test_file_budget_cannot_be_reset_inside_same_transaction(ai_sessions):
    factory, _ = ai_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                await tx.lock(LockPlan(existing=(C,)))
                with tx.file_io():
                    pass
                with tx.file_io():
                    pass


async def test_caught_file_overflow_cannot_commit_prior_factual_write(
    ai_sessions, tmp_path
):
    factory, plain = ai_sessions
    payload = tmp_path / "payload"
    payload.write_bytes(b"oversized")
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                await tx.lock(LockPlan(existing=(C,)))
                await tx.update_factual(
                    C, {"title": "uncommitted"}, expected={"title": "before"}
                )
                with pytest.raises(BusinessException):
                    with tx.file_io(max_payload_bytes=1):
                        read_local(payload)
        with pytest.raises(BusinessException):
            await commit_authorization_session(db)
        await db.rollback()
    async with plain() as db:
        assert await db.scalar(select(AiConversation.title)) == "before"


async def test_factual_cas_miss_poison_prevents_later_success(ai_sessions):
    factory, _ = ai_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                await tx.lock(LockPlan(existing=(C,)))
                with pytest.raises(BusinessException):
                    await tx.update_factual(
                        C, {"title": "after"}, expected={"title": "wrong"}
                    )
                await tx.update_factual(
                    C, {"title": "after"}, expected={"title": "before"}
                )


async def test_no_new_savepoint_after_file_publication(ai_sessions):
    factory, _ = ai_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db, tenants=(TenantLock(0),)
            ) as tx:
                await tx.lock(LockPlan(existing=(C,)))
                with tx.file_io():
                    pass
                async with tx.savepoint():
                    pass
