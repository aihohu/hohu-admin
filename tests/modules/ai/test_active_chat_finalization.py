"""Active streams retain source evidence across finalization and read retries."""

import asyncio

import pytest
from sqlalchemy import select, update

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.db import authorization_chat_owner
from app.db.authorization_chat_owner import ChatOwner
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.authorization_context_candidate import (
    ai_context_candidate,
)
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.system.models.dept import Dept
from app.modules.system.models.user_field_policy import UserFieldPolicyState as State
from tests.modules.ai.test_authorization_chat_lifecycle import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_chat_lifecycle import (
    plan_sessions as plan_sessions,
)
from tests.modules.ai.test_authorization_chat_lifecycle import (
    recovery_case as recovery_case,
)
from tests.modules.ai.test_authorization_chat_lifecycle import (
    system_sessions as system_sessions,
)
from tests.modules.ai.test_authorization_gateway_execution import execute
from tests.modules.system.test_active_transfer_delivery import enable


@pytest.fixture
async def active_turn(execution_case, monkeypatch):
    factory, plain, case = execution_case
    assert (await execute(factory, case)).disposition == "succeeded"
    async with plain() as db:
        await db.execute(
            update(State)
            .where(State.tenant_id == case.tenant.tenant_id)
            .values(mode="legacy", candidate_manifest_digest=None)
        )
        await db.commit()
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    await enable(factory, case.tenant, ())
    owner = ChatOwner(factory)
    source = await owner.source(
        conversation_id=case.claim.conversation_id,
        content="New active turn",
        tenant=case.tenant,
        actor_user_version=1,
    )
    request = {
        "conversation_id": case.claim.conversation_id,
        "source_id": source.message_id,
        "trace_id": source.trace_id,
        "tenant": case.tenant,
        "actor_user_version": 1,
    }
    async with plain() as db:
        frozen = (await db.get(AiMessage, source.message_id)).field_authority
    return owner, plain, request, frozen


@pytest.mark.parametrize("supplied", [False, True])
async def test_active_finalization_preserves_source_evidence(active_turn, supplied):
    owner, plain, request, frozen = active_turn
    lineage = (
        result_projection_service.freeze_lineage(
            tenant=request["tenant"],
            agent_code="dept_mgmt",
            tool_codes=[],
            subject_refs=[],
            field_authority=frozen,
        )
        if supplied
        else None
    )
    message_id = await owner.finalize(
        **request,
        content="Active answer",
        tool_calls=[],
        agent_code="dept_mgmt",
        lineage=lineage,
    )
    projected = await owner.messages(
        conversation_id=request["conversation_id"],
        tenant=request["tenant"],
        actor_user_version=1,
    )
    assert any(item.content == "Active answer" for item in projected)
    async with plain() as db:
        assert (await db.get(AiMessage, message_id)).field_authority == frozen
        await db.execute(update(State).values(epoch=State.epoch + 1))
        await db.commit()
    await owner.finalize(
        **request, content="Reproved answer", tool_calls=[], agent_code="dept_mgmt"
    )
    async with plain() as db:
        assert (await db.get(AiMessage, message_id)).content == "Active answer"
        renewed = (await db.get(AiMessage, message_id)).field_authority
        assert renewed["source_domains"] == frozen["source_domains"]
        assert (
            renewed["authority"]["tenant_epoch"]
            == frozen["authority"]["tenant_epoch"] + 1
        )
        assert (await db.get(AiMessage, request["source_id"])).field_authority == frozen


async def test_check_run_rebuilds_after_context_changes(active_turn, monkeypatch):
    owner, plain, request, frozen = active_turn
    original = ai_context_candidate.recheck
    attempts = []

    async def stale_once(db, tx, inventory):
        attempts.append(db)
        if len(attempts) == 1:
            raise BusinessException(
                409,
                "Concurrent context insert",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        await original(db, tx, inventory)

    monkeypatch.setattr(ai_context_candidate, "recheck", stale_once)
    assert await owner.check_run(**request) == frozen
    assert len(attempts) == 2 and attempts[0] is not attempts[1]
    async with plain() as db:
        assert (
            await db.scalar(
                select(AiMessage.message_id).where(
                    AiMessage.role == "assistant",
                    AiMessage.trace_id == request["trace_id"],
                )
            )
            is None
        )


async def test_check_run_retries_are_bounded(active_turn, monkeypatch):
    owner, _, request, _ = active_turn
    attempts = []

    async def stale(db, _tx, _inventory):
        attempts.append(db)
        raise BusinessException(
            409, "Concurrent context insert", error_code="AUTHORIZATION_SNAPSHOT_STALE"
        )

    monkeypatch.setattr(ai_context_candidate, "recheck", stale)
    with pytest.raises(BusinessException) as caught:
        await owner.check_run(**request)
    assert caught.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
    assert len(attempts) == 3 and len({id(db) for db in attempts}) == 3


@pytest.mark.parametrize("missing", [False, True])
async def test_active_missing_or_revoked_source_is_not_reattested(active_turn, missing):
    owner, plain, request, frozen = active_turn
    async with plain() as db:
        if missing:
            await db.execute(
                update(AiMessage)
                .where(AiMessage.message_id == request["source_id"])
                .values(field_authority=None)
            )
        else:
            await db.execute(
                update(AiMessage)
                .where(AiMessage.message_id == request["source_id"])
                .values(is_active=False)
            )
        await db.commit()
    with pytest.raises(BusinessException):
        await owner.finalize(
            **request, content="Never persist", tool_calls=[], agent_code="dept_mgmt"
        )
    async with plain() as db:
        assert (
            await db.scalar(
                select(AiMessage.message_id).where(
                    AiMessage.role == "assistant",
                    AiMessage.trace_id == request["trace_id"],
                )
            )
            is None
        )
        assert (await db.get(AiMessage, request["source_id"])).field_authority == (
            None if missing else frozen
        )


async def test_retry_rechecks_revocation_in_new_transaction(active_turn, monkeypatch):
    owner, plain, request, frozen = active_turn
    original = ai_context_candidate.recheck
    attempts = []

    async def stale_then_revoke(db, tx, inventory):
        attempts.append(db)
        if len(attempts) == 1:
            raise BusinessException(
                409,
                "Concurrent context insert",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        await original(db, tx, inventory)

    # Revoke only after the first transaction has rolled back and released B.
    original_run = owner._run

    async def run(operation, payload):
        try:
            return await original_run(operation, payload)
        except BusinessException as exc:
            if exc.error_code == "AUTHORIZATION_SNAPSHOT_STALE":
                async with plain() as db:
                    await db.execute(
                        update(AiMessage)
                        .where(AiMessage.message_id == request["source_id"])
                        .values(is_active=False)
                    )
                    await db.commit()
            raise

    monkeypatch.setattr(ai_context_candidate, "recheck", stale_then_revoke)
    monkeypatch.setattr(owner, "_run", run)
    with pytest.raises(BusinessException) as caught:
        await owner.check_run(**request)
    assert caught.value.error_code == "FIELD_POLICY_LOCK_ORDER"
    assert len(attempts) == 2


async def test_check_run_survives_committed_tool_log_insert(active_turn, monkeypatch):
    owner, _, request, frozen = active_turn
    original = ai_context_candidate.discover
    inserted = False
    observations = []

    async def discover(db, tx, **kwargs):
        nonlocal inserted
        inventory = await original(db, tx, **kwargs)
        if not inserted:
            inserted = True
            # A second real owner commits between discovery and acquiring C.
            await owner.start_operation(
                **request,
                tool_name="dept.list",
                agent_code="dept_mgmt",
                tool_call_id="concurrent-log",
                args_hash="a" * 64,
                args_summary="tool=dept.list",
                readonly=True,
                risk="low",
                mode="autonomous",
                injection_hit=False,
            )
        observations.append(inventory)
        return inventory

    monkeypatch.setattr(ai_context_candidate, "discover", discover)
    assert await owner.check_run(**request) == frozen
    assert any(value != observations[-1] for value in observations)


async def test_delivery_replans_after_readonly_result_proof_commits(
    active_turn, monkeypatch
):
    owner, _, request, frozen = active_turn
    log_id = await owner.start_operation(
        **request,
        tool_name="dept.list",
        agent_code="dept_mgmt",
        tool_call_id="delivery-race",
        args_hash="b" * 64,
        args_summary="tool=dept.list",
        readonly=True,
        risk="low",
        mode="autonomous",
        injection_hit=False,
    )
    original = ai_context_candidate.discover
    finished = False
    observations = []

    async def discover(db, tx, **kwargs):
        nonlocal finished
        inventory = await original(db, tx, **kwargs)
        if not finished:
            finished = True
            await owner.finish_operation(
                tenant=request["tenant"],
                log_id=log_id,
                succeeded=True,
                duration_ms=1,
                result_summary="ok",
                target_summary=None,
                error_code=None,
                result_field_authority=frozen,
            )
        observations.append(inventory)
        return inventory

    monkeypatch.setattr(ai_context_candidate, "discover", discover)
    retried = await owner.delivery_check(**request)
    monkeypatch.setattr(ai_context_candidate, "discover", original)
    clean = await owner.delivery_check(**request)
    assert retried["authority"] == clean["authority"] == frozen
    assert any(value != observations[-1] for value in observations)
    assert retried["statements"] > clean["statements"]
    assert retried["decoded_bytes"] > clean["decoded_bytes"]


@pytest.mark.parametrize(
    "error,attempt_count",
    [("AUTHORIZATION_SNAPSHOT_STALE", 3), ("FIELD_AUTHORIZATION_CHANGED", 1)],
)
async def test_delivery_replanning_is_bounded_and_stale_only(
    active_turn, monkeypatch, error, attempt_count
):
    owner, _, request, _ = active_turn
    sessions = []

    async def reject(db, _tx, _inventory):
        assert all(not prior.in_transaction() for prior in sessions)
        sessions.append(db)
        raise BusinessException(409, "Changed proof", error_code=error)

    monkeypatch.setattr(ai_context_candidate, "recheck", reject)
    with pytest.raises(BusinessException) as caught:
        await owner.delivery_check(**request)
    assert caught.value.error_code == error
    assert len(sessions) == attempt_count
    assert len({id(db) for db in sessions}) == attempt_count


async def test_delivery_replanning_cannot_reset_failed_read_budget(
    active_turn, monkeypatch
):
    owner, _, request, _ = active_turn
    budgets = []
    original = authorization_chat_owner.AuthorizationReadBudget

    def capture(**kwargs):
        budget = original(**kwargs)
        budgets.append(budget)
        return budget

    async def stale(_db, _tx, _inventory):
        assert len(budgets) == 1
        budget = budgets[0]
        assert budget.statements > 0
        budget.max_statements = budget.statements + 1
        raise BusinessException(
            409, "Changed proof", error_code="AUTHORIZATION_SNAPSHOT_STALE"
        )

    monkeypatch.setattr(authorization_chat_owner, "AuthorizationReadBudget", capture)
    monkeypatch.setattr(ai_context_candidate, "recheck", stale)
    with pytest.raises(BusinessException) as caught:
        await owner.delivery_check(**request)
    assert caught.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
    assert len(budgets) == 1


async def test_delivery_replanning_uses_remaining_deadline(active_turn, monkeypatch):
    owner, _, request, frozen = active_turn
    transactions = authorization_chat_owner.authorization_transactions
    original_scope = transactions.scope
    original_recheck = ai_context_candidate.recheck
    timeouts = []
    attempts = 0

    def scope(*args, **kwargs):
        timeouts.append(kwargs["budget"].timeout_ms)
        return original_scope(*args, **kwargs)

    async def stale_once(db, tx, inventory):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await asyncio.sleep(0.08)
            raise BusinessException(
                409, "Changed proof", error_code="AUTHORIZATION_SNAPSHOT_STALE"
            )
        await original_recheck(db, tx, inventory)

    monkeypatch.setattr(transactions, "scope", scope)
    monkeypatch.setattr(ai_context_candidate, "recheck", stale_once)
    assert (await owner.delivery_check(**request))["authority"] == frozen
    assert len(timeouts) == 2
    assert 0 < timeouts[1] <= timeouts[0] - 70 < 8000


@pytest.mark.parametrize("revoke_first", [False, True])
async def test_active_write_does_not_revoke_itself(
    execution_case, monkeypatch, revoke_first
):
    factory, plain, case = execution_case
    async with plain() as db:
        await db.execute(
            update(State)
            .where(State.tenant_id == 0)
            .values(mode="legacy", candidate_manifest_digest=None)
        )
        await db.commit()
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    await enable(factory, case.tenant, ())
    frozen = await ChatOwner(factory).model_authority(
        tenant=case.tenant, actor_user_version=1
    )
    async with plain() as db:
        await db.execute(update(AiPreparedAction).values(field_authority=frozen))
        await db.execute(
            update(AiMessage)
            .where(AiMessage.role == "user")
            .values(field_authority=frozen)
        )
        if revoke_first:
            await db.execute(
                update(State).where(State.tenant_id == 0).values(epoch=State.epoch + 1)
            )
        await db.commit()
    result = await execute(factory, case)
    assert result.disposition == ("failed" if revoke_first else "succeeded")
    async with plain() as db:
        departments = list(await db.scalars(select(Dept)))
        assert len(departments) == (0 if revoke_first else 1)
        action = await db.get(AiPreparedAction, case.claim.action_id)
        assert action.field_authority == frozen
        assert (await db.get(State, 0)).epoch == frozen["authority"]["tenant_epoch"] + 1
