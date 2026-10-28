"""Stream completion must be owned by a fresh authorization transaction."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select
from starlette.requests import Request

from app.core.exceptions import BusinessException
from app.db import authorization_chat_owner
from app.db.session import AsyncSessionLocal, engine
from app.modules.ai.api import chat as chat_module
from app.modules.ai.api.chat import _finalize_stream_turn
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.models.routing_log import AiRoutingLog
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.auth.service import access_identity
from app.modules.system.models.user import User
from tests.modules.ai.test_atomic_prepared_completion import (
    approve,
    assert_success,
    authenticated_actor,
    count_real_handler,
    observe,
)
from tests.modules.ai.test_atomic_prepared_completion import (
    completion_case as completion_case,
)
from tests.modules.ai.test_completion_read_projection import stored_facts


async def test_actual_chat_stream_persists_after_source_and_routing_commits(
    completion_case, monkeypatch
):
    await run_chat(completion_case, monkeypatch)


async def run_chat(
    completion_case, monkeypatch, *, egress_check=None, agent_code="user_mgmt"
):
    case = completion_case
    assert (await approve(case, decision="reject")).data.status == "rejected"
    trace_id = "tr_" + uuid4().hex
    body = {
        "trigger": "submit-message",
        "id": "stream-owner-test",
        "conversationId": str(case.conversation_id),
        "traceId": trace_id,
        "agentCode": agent_code,
        "messages": [
            {
                "id": "new-source",
                "role": "user",
                "parts": [{"type": "text", "text": "Hello"}],
            }
        ],
    }

    async def receive():
        return {
            "type": "http.request",
            "body": json.dumps(body).encode(),
            "more_body": False,
        }

    class Adapter:
        build_run_input = staticmethod(chat_module.VercelAIAdapter.build_run_input)

        def __init__(self, **_kwargs):
            pass

        def run_stream(self, **_kwargs):
            return None

        async def encode_stream(self, _stream):
            yield 'data: {"type":"text-delta","id":"answer","delta":"Hello there"}\n\n'

    monkeypatch.setattr(chat_module, "VercelAIAdapter", Adapter)
    monkeypatch.setattr(
        chat_module.chat_service, "create_agent", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(
        provider_egress,
        "is_model_allowed",
        egress_check or AsyncMock(return_value=True),
    )
    try:
        async with AsyncSessionLocal() as db:
            actor = await authenticated_actor(case, db)
            body["modelId"] = str(
                await db.scalar(
                    select(AiModel.model_id).where(
                        AiModel.provider_id == case.provider_id
                    )
                )
            )
            response = await chat_module.chat(
                Request(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/ai/chat",
                        "headers": [],
                        "client": ("127.0.0.1", 1234),
                    },
                    receive,
                ),
                db=db,
                _current_user=actor,
            )
            chunks = [chunk async for chunk in response.body_iterator]
            assert db._request_released and not db._request_wrote
            with pytest.raises(BusinessException):
                await db.execute(select(AiMessage.message_id).limit(1))
        frames = [
            json.loads(line[6:])
            for chunk in chunks
            for line in chunk.splitlines()
            if line.startswith("data: {")
        ]
        done = next(frame for frame in frames if frame.get("type") == "done")
        assert done["persistence"] == "committed", frames
        async with AsyncSessionLocal() as db:
            message = await db.scalar(
                select(AiMessage).where(
                    AiMessage.tenant_id == case.tenant.tenant_id,
                    AiMessage.conversation_id == case.conversation_id,
                    AiMessage.trace_id == trace_id,
                    AiMessage.role == "assistant",
                )
            )
            assert message is not None
            assert message.content == "Hello there"
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                delete(AiRoutingLog).where(
                    AiRoutingLog.tenant_id == case.tenant.tenant_id,
                    AiRoutingLog.trace_id == trace_id,
                )
            )


@pytest.mark.parametrize("flushed", [False, True])
async def test_stream_finalizer_rejects_an_active_legacy_writer(
    completion_case, monkeypatch, flushed
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    assert (await approve(case)).data.status == "succeeded"
    before = await stored_facts(case)
    async with AsyncSessionLocal() as db:
        identity = access_identity(await authenticated_actor(case, db))
        action = await db.get(AiPreparedAction, case.action_id)
        conversation = await db.get(AiConversation, case.conversation_id)
        conversation.title = "Uncommitted legacy write"
        if flushed:
            await db.flush()
        events = await _finalize_stream_turn(
            db,
            conversation_id=case.conversation_id,
            trace_id=action.trace_id,
            source_user_message_id=action.source_user_message_id,
            content="Do not commit the old writer",
            tool_calls=[],
            agent_code=action.agent_code,
            stream_error_code=None,
            tenant=case.tenant,
            actor_user_version=identity.user_version,
        )
        assert events[-1].persistence == "failed"
    assert await stored_facts(case) == before
    assert len(case.calls) == 1


async def test_stream_finalizer_enters_global_guard_before_context(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    assert (await approve(case)).data.status == "succeeded"
    locks = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        if "FOR UPDATE" in sql or "FOR SHARE" in sql:
            locks.append(sql)

    async with AsyncSessionLocal() as db:
        identity = access_identity(await authenticated_actor(case, db))
        action = await db.get(AiPreparedAction, case.action_id)
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            events = await _finalize_stream_turn(
                db,
                conversation_id=case.conversation_id,
                trace_id=action.trace_id,
                source_user_message_id=action.source_user_message_id,
                content="The operation has a saved result.",
                tool_calls=[],
                agent_code=action.agent_code,
                stream_error_code=None,
                tenant=case.tenant,
                actor_user_version=identity.user_version,
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert events[-1].persistence == "committed"
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    assert len(case.calls) == 1


@pytest.mark.parametrize("fault", ["version", "scope", "hash", "tenant"])
async def test_stream_finalizer_rechecks_original_authority_and_lineage(
    completion_case, monkeypatch, fault
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    assert (await approve(case)).data.status == "succeeded"
    before = await stored_facts(case)
    async with AsyncSessionLocal() as db:
        identity = access_identity(await authenticated_actor(case, db))
    lineage = result_projection_service.freeze_lineage(
        tenant=case.tenant, agent_code="user_mgmt", tool_codes=[], subject_refs=[]
    )
    if fault == "scope":
        lineage = replace(lineage, data_scope_hash="stale-scope")
    elif fault == "hash":
        lineage = replace(lineage, subject_refs_hash="f" * 64)
    elif fault == "tenant":
        lineage = replace(lineage, tenant_id=999999)
    try:
        if fault == "version":
            async with AsyncSessionLocal() as db, db.begin():
                user = await db.get(User, case.tenant.actor_user_id)
                user.auth_version = identity.user_version + 1
        async with AsyncSessionLocal() as db:
            action = await db.get(AiPreparedAction, case.action_id)
            events = await _finalize_stream_turn(
                db,
                conversation_id=case.conversation_id,
                trace_id=action.trace_id,
                source_user_message_id=action.source_user_message_id,
                content="Do not persist stale authorized content",
                tool_calls=[],
                agent_code="user_mgmt",
                stream_error_code=None,
                tenant=case.tenant,
                actor_user_version=identity.user_version,
                lineage=lineage,
            )
        assert events[-1].persistence == "failed"
        assert await stored_facts(case) == before
        assert len(case.calls) == 1
    finally:
        if fault == "version":
            async with AsyncSessionLocal() as db, db.begin():
                user = await db.get(User, case.tenant.actor_user_id)
                user.auth_version = identity.user_version


async def test_stream_unknown_commit_never_sends_committed_or_reexecutes(
    completion_case, monkeypatch
):
    case = completion_case
    count_real_handler(case, monkeypatch)
    assert (await approve(case)).data.status == "succeeded"
    original = authorization_chat_owner.commit_authorization_session
    commits = []

    async def unknown(db):
        await original(db)
        commits.append(True)
        raise BusinessException(
            503, "unknown", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )

    monkeypatch.setattr(
        authorization_chat_owner, "commit_authorization_session", unknown
    )
    async with AsyncSessionLocal() as db:
        identity = access_identity(await authenticated_actor(case, db))
        action = await db.get(AiPreparedAction, case.action_id)
        events = await _finalize_stream_turn(
            db,
            conversation_id=case.conversation_id,
            trace_id=action.trace_id,
            source_user_message_id=action.source_user_message_id,
            content="Saved business outcome",
            tool_calls=[],
            agent_code="user_mgmt",
            stream_error_code=None,
            tenant=case.tenant,
            actor_user_version=identity.user_version,
        )
    assert commits == [True]
    assert events[-1].persistence == "failed"
    assert len(case.calls) == 1
    assert_success(case, await observe(case))
