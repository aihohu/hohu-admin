"""The actual chat prefix must never insert facts through the request session."""

import asyncio
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select

from app.core.exceptions import BusinessException
from app.db import authorization_chat_owner
from app.db.authorization_owner import _QUARANTINED
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.session import engine, is_session_quarantined
from app.modules.ai.api import chat as chat_module
from app.modules.ai.api.chat import _finish_chat_read_phase
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.routing_log import AiRoutingLog
from app.modules.ai.service.authorization_chat_ingress_candidate import (
    freeze_run_authority,
)
from app.modules.ai.service.chat_service import chat_service
from app.modules.auth.service import access_identity
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal
from tests.modules.ai.test_atomic_prepared_completion import (
    approve,
    authenticated_actor,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_authorization_stream_finalize import (
    test_actual_chat_stream_persists_after_source_and_routing_commits as run_actual_chat,
)


async def test_actual_chat_source_and_routing_use_planned_writers(
    completion_case, monkeypatch
):
    writes = []

    def capture(_connection, _cursor, sql, _params, context, _many):
        statement = context.compiled.statement if context.compiled else None
        table = getattr(statement, "table", None)
        if sql.startswith("INSERT") and getattr(table, "name", "") in {
            "ai_routing_log",
            "ai_message",
        }:
            writes.append(
                (
                    table.name,
                    context.execution_options.get("_authorization_plan_permit")
                    is not None,
                )
            )

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await run_actual_chat(completion_case, monkeypatch)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert {name for name, _ in writes} == {"ai_routing_log", "ai_message"}
    assert all(planned for _, planned in writes), writes


@pytest.fixture
async def ingress(completion_case):
    case = completion_case
    assert (await approve(case, decision="reject")).data.status == "rejected"
    trace = "tr_" + uuid4().hex
    async with AsyncSessionLocal() as db:
        actor = await authenticated_actor(case, db)
        identity = access_identity(actor)
        deps = await chat_service.build_chat_deps(
            db,
            actor,
            agent_code="user_mgmt",
            conversation_id=case.conversation_id,
            trace_id=trace,
        )
        model_id = await db.scalar(
            select(AiModel.model_id).where(
                AiModel.provider_id == case.provider_id,
            )
        )
        authority = await freeze_run_authority(db, deps=deps, model_name=str(model_id))
    request = {
        "tenant": identity.tenant,
        "actor_user_version": identity.user_version,
        "conversation_id": case.conversation_id,
        "trace_id": trace,
        "content": "A new question",
        "parts": [{"type": "text", "text": "A new question"}],
        "authority": authority,
        "audit": {
            "input_message": "A new question",
            "candidates": ["user_mgmt"],
            "llm_choice": None,
            "final_agent": "user_mgmt",
            "reason": "manual_override",
            "latency_ms": 0,
        },
    }
    try:
        yield request
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                delete(AiRoutingLog).where(
                    AiRoutingLog.tenant_id == case.tenant.tenant_id,
                    AiRoutingLog.trace_id == trace,
                )
            )


async def ingress_facts(request):
    async with AsyncSessionLocal() as db:
        sources = list(
            await db.scalars(
                select(AiMessage).where(
                    AiMessage.tenant_id == request["tenant"].tenant_id,
                    AiMessage.conversation_id == request["conversation_id"],
                    AiMessage.trace_id == request["trace_id"],
                )
            )
        )
        routes = list(
            await db.scalars(
                select(AiRoutingLog).where(
                    AiRoutingLog.tenant_id == request["tenant"].tenant_id,
                    AiRoutingLog.trace_id == request["trace_id"],
                )
            )
        )
        return sources, routes


async def test_source_and_audit_are_one_guarded_transaction(ingress):
    locks = []

    def capture(_connection, _cursor, sql, _params, _context, _many):
        if "FOR UPDATE" in sql or "FOR SHARE" in sql:
            locks.append(sql)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        source_id = await AuthorizationRuntime(engine).chat.start_run(**ingress)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert "sys_user_field_policy_global_guard" in locks[0]
    assert "sys_user_field_policy_state" in locks[1] and "FOR UPDATE" in locks[1]
    sources, routes = await ingress_facts(ingress)
    assert len(sources) == len(routes) == 1
    assert sources[0].message_id == source_id
    assert sources[0].parts == ingress["parts"]
    assert sources[0].agent_code == "user_mgmt"
    assert routes[0].input_message_hash != ingress["content"]


@pytest.mark.parametrize(
    "fault", ["version", "scope", "dependencies", "agent_snapshot", "model"]
)
async def test_source_rejects_changed_original_authority(ingress, fault):
    request = dict(ingress)
    authority = request["authority"]
    if fault == "version":
        request["actor_user_version"] += 1
    elif fault == "scope":
        request["authority"] = replace(authority, scope_hash="different")
    elif fault == "dependencies":
        request["authority"] = replace(authority, dependencies=(2**62,))
    elif fault == "agent_snapshot":
        request["authority"] = replace(authority, agent_facts=b"{}")
    else:
        async with AsyncSessionLocal() as db, db.begin():
            model = await db.get(AiModel, authority.model.model_id)
            model.is_enabled = False
    try:
        with pytest.raises(BusinessException):
            await AuthorizationRuntime(engine).chat.start_run(**request)
        assert await ingress_facts(ingress) == ([], [])
    finally:
        if fault == "model":
            async with AsyncSessionLocal() as db, db.begin():
                model = await db.get(AiModel, authority.model.model_id)
                model.is_enabled = True


async def test_same_trace_concurrent_source_is_single_claim(ingress):
    results = await asyncio.gather(
        AuthorizationRuntime(engine).chat.start_run(**ingress),
        AuthorizationRuntime(engine).chat.start_run(**ingress),
        return_exceptions=True,
    )
    assert sum(type(value) is int for value in results) == 1
    error = next(value for value in results if isinstance(value, BusinessException))
    assert error.error_code == "AI_CHAT_TRACE_CONFLICT"
    sources, routes = await ingress_facts(ingress)
    assert len(sources) == len(routes) == 1


@pytest.mark.parametrize("after", [False, True])
async def test_source_unknown_ack_is_never_retried(ingress, monkeypatch, after):
    original = authorization_chat_owner.commit_authorization_session
    calls = []

    async def unknown(db):
        calls.append(True)
        if after:
            await original(db)
        raise BusinessException(
            503, "unknown", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )

    monkeypatch.setattr(
        authorization_chat_owner, "commit_authorization_session", unknown
    )
    with pytest.raises(BusinessException, match="unknown"):
        await AuthorizationRuntime(engine).chat.start_run(**ingress)
    assert calls == [True]
    sources, routes = await ingress_facts(ingress)
    assert len(sources) == len(routes) == int(after)


async def test_read_phase_rollback_is_bounded_and_never_reused(ingress, monkeypatch):
    release = asyncio.Event()
    db = AsyncSessionLocal()
    await db.execute(
        select(AiMessage.message_id).where(
            AiMessage.tenant_id == ingress["tenant"].tenant_id,
            AiMessage.conversation_id == ingress["conversation_id"],
        )
    )
    original = db.rollback

    async def blocked():
        await original()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    monkeypatch.setattr(db, "rollback", blocked)
    task = asyncio.create_task(_finish_chat_read_phase(db))
    try:
        completed, _ = await asyncio.wait({task}, timeout=0.8)
        assert task in completed
        with pytest.raises(BusinessException):
            task.result()
        assert is_session_quarantined(db)
        with pytest.raises(BusinessException):
            await db.execute(select(AiMessage.message_id).limit(1))
        with pytest.raises(BusinessException):
            await _finish_chat_read_phase(db)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        async with asyncio.timeout(2):
            held = _QUARANTINED.get(id(db))
            if held is not None:
                await asyncio.shield(held[2])
        await db.close()


@pytest.mark.parametrize("after", [False, True])
async def test_actual_chat_does_not_start_stream_after_unknown_source(
    completion_case,
    monkeypatch,
    after,
):
    original_start = authorization_chat_owner.ChatOwner.start_run
    original_commit = authorization_chat_owner.commit_authorization_session
    calls = []

    async def start(owner, **request):
        async def unknown(db):
            calls.append(True)
            if after:
                await original_commit(db)
            raise BusinessException(
                503, "unknown", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
            )

        with monkeypatch.context() as scoped:
            scoped.setattr(
                authorization_chat_owner, "commit_authorization_session", unknown
            )
            return await original_start(owner, **request)

    stream = Mock(side_effect=AssertionError("unknown source cannot start a stream"))
    monkeypatch.setattr(authorization_chat_owner.ChatOwner, "start_run", start)
    monkeypatch.setattr(chat_module, "StreamingResponse", stream)
    with pytest.raises(BusinessException, match="unknown"):
        await run_actual_chat(completion_case, monkeypatch)
    assert calls == [True]
    stream.assert_not_called()
