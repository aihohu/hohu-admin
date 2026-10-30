"""A rejected preflight must not become an unresolved business execution."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import make_transient

from app.core import redis as redis_module
from app.core.exceptions import BusinessException
from app.db import authorization_chat_owner
from app.modules.ai.agents.gateway import executor
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.chat_run_service import chat_run_finalizer
from app.modules.ai.service.chat_service import chat_service
from app.modules.ai.service.completion_read_state_service import (
    UNKNOWN_COMPLETION,
    EffectiveCompletion,
    completion_read_state_service,
    project_verified_content,
)
from app.modules.ai.service.conversation_service import conversation_service
from app.modules.ai.service.operation_status_projection import (
    project_operation as get_operation_log,
)
from app.modules.ai.service.preflight_completion_service import (
    MARKER,
    preflight_fingerprint,
)
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.ai.service.trace_service import trace_service
from app.modules.ai.service.user_delete_dependency_service import (
    user_delete_dependency_service,
)
from app.modules.auth.service import load_live_user_authority
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import authenticated_actor
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)


async def reject_preflight(
    case,
    monkeypatch,
    expected_code="AI_PREVIEW_REJECTED",
    *,
    missing_log=False,
):
    async with AsyncSessionLocal() as db, db.begin():
        source = await db.scalar(
            select(AiMessage).where(
                AiMessage.conversation_id == case.conversation_id,
                AiMessage.role == "user",
            )
        )
        source_id, trace_id = source.message_id, source.trace_id
        await db.execute(
            delete(AiPreparedAction).where(AiPreparedAction.action_id == case.action_id)
        )
        await db.execute(
            delete(AiOperationLog).where(AiOperationLog.log_id == case.log_id)
        )
    registered = ToolRegistry.get().find("user.create")
    handler = AsyncMock(side_effect=AssertionError("Business handler must not run"))
    monkeypatch.setattr(registered, "fn", handler)
    # Use the real duplicate-name preflight through the fixed adapter.
    case.args = {**case.args, "user_name": "admin"}
    # Quota behavior has its own integration suite; don't spend a shared
    # actor's rolling budget across these protocol-corruption cases.
    monkeypatch.setattr(
        executor, "check_l1_rate_limit", AsyncMock(return_value=(0, None))
    )
    monkeypatch.setattr(
        executor, "check_l1_global_rate_limit", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(executor, "check_l2_daily_quota", AsyncMock())
    monkeypatch.setattr(executor, "redis_client", redis_module.redis_client)
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        deps = await chat_service.build_chat_deps(
            db,
            actor,
            agent_code="user_mgmt",
            trace_id=trace_id,
            conversation_id=case.conversation_id,
        )
        deps.conversation_id = case.conversation_id
        deps.source_user_message_id = source_id
        db.expunge_all()
        await db.rollback()
        result = await executor.execute_tool("user.create", case.args, deps)
    assert result.error_code == expected_code
    handler.assert_not_awaited()
    async with AsyncSessionLocal() as db:
        log = await db.scalar(
            select(AiOperationLog).where(
                AiOperationLog.conversation_id == case.conversation_id
            )
        )
        if missing_log:
            assert log is None
            return None
        case.log_id, case.tool_call_id = log.log_id, log.tool_call_id
        assert (
            await db.scalar(
                select(AiPreparedAction.action_id).where(
                    AiPreparedAction.conversation_id == case.conversation_id
                )
            )
            is None
        )
    return log


async def test_gateway_preflight_rejection_allows_next_message(
    completion_case, monkeypatch
):
    case = completion_case
    log = await reject_preflight(case, monkeypatch)
    states = await completion_read_state_service.read(
        tenant=case.tenant,
        conversation_id=case.conversation_id,
        user_id=case.tenant.actor_user_id,
    )
    state = states[log.tool_call_id]
    assert state.verified and not state.unknown
    assert state.status == "failed" and state.error_code == "AI_PREVIEW_REJECTED"
    async with AsyncSessionLocal() as db:
        assert not await prepared_action_service.has_in_progress_for_conversation(
            db,
            conversation_id=case.conversation_id,
            user_id=case.tenant.actor_user_id,
            tenant=case.tenant,
        )
        actor = await load_live_user_authority(db, tenant=case.tenant)
        poll = await get_operation_log(
            tool_call_id=log.tool_call_id, db=db, current_user=actor, tenant=case.tenant
        )
        trace = await trace_service.get_trace(
            db, tenant=case.tenant, trace_id=log.trace_id
        )
        assert (
            poll.data.error_code
            == trace.operations[0].error_code
            == "AI_PREVIEW_REJECTED"
        )
        await chat_run_finalizer.finalize_assistant_turn(
            db,
            conversation_id=case.conversation_id,
            trace_id=log.trace_id,
            source_user_message_id=log.source_user_message_id,
            content=f"Preflight refused.\n[AI_EXECUTION_OUTCOME_UNKNOWN] toolCallId={log.tool_call_id}",
            tool_calls=[
                {"tool": "user.create", "tool_call_id": log.tool_call_id, "ok": True}
            ],
            agent_code="user_mgmt",
            tenant=case.tenant,
            lineage=result_projection_service.freeze_lineage(
                tenant=case.tenant,
                agent_code="user_mgmt",
                tool_codes=["user.create"],
                subject_refs=[],
            ),
        )
        await db.commit()
        history = await conversation_service.project_messages(
            db, conversation_id=case.conversation_id, current_user=actor
        )
        assistant = next(row for row in history if row.role == "assistant")
        assert assistant.tool_calls[0]["ok"] is False
        assert assistant.tool_calls[0]["error_code"] == "AI_PREVIEW_REJECTED"
        assert "AI_EXECUTION_OUTCOME_UNKNOWN" not in assistant.content
        stored_message = await db.scalar(
            select(AiMessage).where(
                AiMessage.conversation_id == case.conversation_id,
                AiMessage.role == "assistant",
            )
        )
        assert (
            f"[AI_EXECUTION_OUTCOME_UNKNOWN] toolCallId={log.tool_call_id}"
            in stored_message.content
        )
        # Exercise both deletion dependency gates without deleting anything.
        assert (
            await prepared_action_service.expire_for_conversation_delete(
                db,
                conversation_id=case.conversation_id,
                user_id=actor.user_id,
                tenant=case.tenant,
            )
            == []
        )
        await user_delete_dependency_service._assert_resolved(
            db,
            SimpleNamespace(target_action_ids=(), target_log_ids=(log.log_id,)),
            tenant=case.tenant,
        )
    async with AsyncSessionLocal() as db:
        refreshed = await db.get(AiOperationLog, log.log_id)
        assert refreshed.result_summary == log.result_summary


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "malformed",
        "copied",
        "source",
        "trace",
        "hash",
        "error",
        "started",
        "approved",
        "confirmation",
        "result",
        "phase",
        "origin",
        "annotation",
        "bound_pa",
        "duplicate_marker",
        "duplicate_phase",
        "duplicate_fact",
        "readonly",
    ],
)
async def test_preflight_proof_never_unlocks_conflicting_facts(
    completion_case, monkeypatch, corruption
):
    case = completion_case
    async with AsyncSessionLocal() as db:
        action = await db.get(AiPreparedAction, case.action_id)
        db.expunge(action)
    log = await reject_preflight(case, monkeypatch)
    async with AsyncSessionLocal() as db, db.begin():
        row = await db.get(AiOperationLog, log.log_id)
        proof = json.loads(row.result_summary)
        if corruption == "missing":
            row.result_summary = None
        elif corruption == "malformed":
            row.result_summary = '{"' + MARKER + '":[]}'
        elif corruption == "annotation":
            row.result_summary += "__v0162_reconciliation_v1"
        elif corruption == "copied":
            proof[MARKER]["facts"]["log_id"] += 1
            row.result_summary = json.dumps(proof)
        elif corruption in {"phase", "origin"}:
            proof[MARKER][corruption] = "unknown"
            row.result_summary = json.dumps(proof)
        elif corruption == "source":
            row.source_user_message_id = None
        elif corruption == "trace":
            row.trace_id = "wrong-trace"
        elif corruption == "hash":
            row.args_hash = "f" * 64
        elif corruption == "error":
            row.error_code = "AI_INTERNAL_ERROR"
        elif corruption == "started":
            row.started_at = row.queued_at
        elif corruption == "approved":
            row.approved_by = case.tenant.actor_user_id
        elif corruption == "confirmation":
            row.confirmation_id = "unexpected"
        elif corruption == "result":
            row.target_summary = "[]"
        elif corruption == "readonly":
            row.readonly_snapshot = True
        elif corruption == "duplicate_marker":
            row.result_summary = '{"' + MARKER + '":null,' + row.result_summary[1:]
        elif corruption == "duplicate_phase":
            row.result_summary = row.result_summary.replace(
                '"phase": "dry_run"', '"phase":"execution","phase":"dry_run"'
            )
        elif corruption == "duplicate_fact":
            row.result_summary = row.result_summary.replace(
                '"tool_name": "user.create"',
                '"tool_name":"other","tool_name":"user.create"',
            )
        elif corruption == "bound_pa":
            make_transient(action)
            action.execute_tool_call_id = log.tool_call_id
            db.add(action)
    states = await completion_read_state_service.read(
        tenant=case.tenant,
        conversation_id=case.conversation_id,
        user_id=case.tenant.actor_user_id,
    )
    assert states[log.tool_call_id].unknown
    async with AsyncSessionLocal() as db:
        assert await prepared_action_service.has_in_progress_for_conversation(
            db,
            conversation_id=case.conversation_id,
            user_id=case.tenant.actor_user_id,
            tenant=case.tenant,
        )


async def test_preflight_proof_is_tenant_scoped(completion_case, monkeypatch):
    case = completion_case
    await reject_preflight(case, monkeypatch)
    assert (
        await completion_read_state_service.read(
            tenant=replace(case.tenant, tenant_id=999),
            conversation_id=case.conversation_id,
            user_id=case.tenant.actor_user_id,
        )
        == {}
    )


@pytest.mark.parametrize("remaining", [True, False])
def test_resolved_hint_projection_preserves_other_unknown_references(remaining):
    notice = "操作结果尚待核对，请勿重新办理；请刷新会话或联系管理员核对。"
    content = (
        f"Original body.\n{notice}\n[AI_EXECUTION_OUTCOME_UNKNOWN] toolCallId=tc_one"
    )
    calls = [{"tool_call_id": "tc_one"}]
    states = {
        "tc_one": EffectiveCompletion(
            "failed", "AI_PREVIEW_REJECTED", None, 0, verified=True
        )
    }
    if remaining:
        content += "\n[AI_EXECUTION_OUTCOME_UNKNOWN] toolCallId=tc_one_longer"
        calls.append({"tool_call_id": "tc_one_longer"})
        states["tc_one_longer"] = UNKNOWN_COMPLETION
    projected = project_verified_content(content, calls, states)
    assert (
        "[AI_EXECUTION_OUTCOME_UNKNOWN] toolCallId=tc_one" not in projected.splitlines()
    )
    assert (notice in projected) is remaining
    assert ("tc_one_longer" in projected) is remaining
    assert "Original body." in projected
    assert project_verified_content(content, calls, {}) == content


async def test_real_duplicate_username_preflight_never_executes(
    completion_case, monkeypatch
):
    case = completion_case
    case.args = {**case.args, "user_name": "admin"}
    log = await reject_preflight(case, monkeypatch)
    state = (
        await completion_read_state_service.read(
            tenant=case.tenant, tool_call_ids=[log.tool_call_id]
        )
    )[log.tool_call_id]
    assert state.verified and state.error_code == "AI_PREVIEW_REJECTED"


async def test_preflight_does_not_supply_missing_whole_turn_authority(
    completion_case, monkeypatch
):
    case = completion_case
    log = await reject_preflight(case, monkeypatch)
    async with AsyncSessionLocal() as db:
        message = await chat_run_finalizer.finalize_assistant_turn(
            db,
            conversation_id=case.conversation_id,
            trace_id=log.trace_id,
            source_user_message_id=log.source_user_message_id,
            content="A prior restricted result must not gain authority from this failure.",
            tool_calls=[
                {"tool": "user.create", "tool_call_id": log.tool_call_id},
                {
                    "tool": "user.lookup",
                    "tool_call_id": "unproven_read",
                    "ok": True,
                    "result": {"private": "value"},
                },
            ],
            agent_code="user_mgmt",
            tenant=case.tenant,
            lineage=None,
            projection_dependency_message_ids=[log.source_user_message_id],
        )
        assert result_projection_service.lineage_from_record(message) is None
        assert message.projection_dependency_message_ids == [
            str(log.source_user_message_id)
        ]
        await db.commit()
        actor = await load_live_user_authority(db, tenant=case.tenant)
        history = await conversation_service.project_messages(
            db, conversation_id=case.conversation_id, current_user=actor
        )
        assistant = next(row for row in history if row.role == "assistant")
        assert not hasattr(assistant, "tool_calls")


@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
async def test_preflight_proof_and_failure_share_transaction(
    completion_case, monkeypatch, boundary
):
    case = completion_case
    attempts = []
    original_commit = authorization_chat_owner.commit_authorization_session

    async def unknown(db):
        attempts.append(True)
        if boundary == "after_commit":
            await original_commit(db)
        raise BusinessException(
            503, "lost ACK", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )

    monkeypatch.setattr(
        authorization_chat_owner, "commit_authorization_session", unknown
    )
    log = await reject_preflight(
        case, monkeypatch, "AI_INTERNAL_ERROR", missing_log=boundary == "before_commit"
    )
    assert attempts == [True]
    if boundary == "before_commit":
        assert log is None
    else:
        states = await completion_read_state_service.read(
            tenant=case.tenant, tool_call_ids=[log.tool_call_id]
        )
        assert (
            states[log.tool_call_id].verified and not states[log.tool_call_id].unknown
        )


@pytest.mark.parametrize("corrupt", [False, True])
async def test_explicit_maintenance_proof_preserves_original_facts(
    completion_case, monkeypatch, corrupt
):
    case = completion_case
    log = await reject_preflight(case, monkeypatch)
    async with AsyncSessionLocal() as db, db.begin():
        row = await db.get(AiOperationLog, log.log_id)
        proof = json.loads(row.result_summary)
        proof[MARKER]["origin"] = "maintenance_attestation"
        proof[MARKER]["attestation"] = {
            "operator": "isolated-test-review",
            "recorded_at": "2026-10-22T00:00:00+00:00",
            "database_identity": "isolated-test",
            "source_sha256": "a" * 64,
            "evidence_sha256": "b" * 64,
            "original_fingerprint": "c" * 64 if corrupt else preflight_fingerprint(row),
            "previous_summary": None,
        }
        row.result_summary = json.dumps(proof)
    state = (
        await completion_read_state_service.read(
            tenant=case.tenant, tool_call_ids=[log.tool_call_id]
        )
    )[log.tool_call_id]
    assert state.unknown is corrupt
    async with AsyncSessionLocal() as db:
        row = await db.get(AiOperationLog, log.log_id)
        assert preflight_fingerprint(row) == preflight_fingerprint(log)
