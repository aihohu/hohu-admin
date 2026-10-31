"""Read DTOs use current identity and one protected snapshot without writes."""

import pytest
from sqlalchemy import event, update

from app.db import authorization_http
from app.modules.ai.api import operation_log
from app.modules.system.models.user import User
from tests.modules.ai.test_authorization_conversation_http import (
    chat_model_case as chat_model_case,
)
from tests.modules.ai.test_authorization_conversation_http import (
    conversation_http as conversation_http,
)
from tests.modules.ai.test_authorization_conversation_http import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_conversation_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.ai.test_authorization_conversation_http import (
    recovery_case as recovery_case,
)
from tests.modules.ai.test_authorization_conversation_http import (
    system_sessions as system_sessions,
)


@pytest.mark.parametrize(
    "surface", ["list", "detail", "trace-list", "trace-detail", "poll"]
)
async def test_ai_reads_have_global_first_and_no_commit(conversation_http, surface):
    client, plain, case = conversation_http
    client._transport.app.include_router(
        operation_log.router, prefix="/ai/operation-log"
    )
    paths = {
        "list": "/ai/conversation/list",
        "detail": f"/ai/conversation/{case.conversation.conversation_id}",
        "trace-list": "/ai/operation-log/traces",
        "poll": f"/ai/operation-log?tool_call_id={case.action.execute_tool_call_id}",
        "trace-detail": f"/ai/operation-log/traces/{case.action.trace_id}",
    }
    locks, commits = [], []
    engine = plain.kw["bind"].sync_engine

    def sql(_c, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    def commit(connection):
        commits.append(connection)

    event.listen(engine, "before_cursor_execute", sql)
    event.listen(engine, "commit", commit)
    try:
        response = await client.get(paths[surface])
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        event.remove(engine, "commit", commit)
    assert response.status_code == 200, response.text
    assert not commits
    assert locks and "sys_user_field_policy_global_guard" in locks[0]


@pytest.mark.parametrize(
    "surface", ["list", "detail", "trace-list", "trace-detail", "poll"]
)
async def test_ai_read_rechecks_identity_after_http_auth(
    conversation_http, monkeypatch, surface
):
    client, plain, case = conversation_http
    client._transport.app.include_router(
        operation_log.router, prefix="/ai/operation-log"
    )
    paths = {
        "list": "/ai/conversation/list",
        "detail": f"/ai/conversation/{case.conversation.conversation_id}",
        "trace-list": "/ai/operation-log/traces",
        "poll": f"/ai/operation-log?tool_call_id={case.action.execute_tool_call_id}",
        "trace-detail": f"/ai/operation-log/traces/{case.action.trace_id}",
    }
    original = authorization_http.release_request_reads

    async def release(db):
        await original(db)
        async with plain() as revoke:
            await revoke.execute(
                update(User).where(User.user_id == 101).values(auth_version=2)
            )
            await revoke.commit()

    monkeypatch.setattr(authorization_http, "release_request_reads", release)
    response = await client.get(paths[surface])
    assert response.status_code == 401, response.text
