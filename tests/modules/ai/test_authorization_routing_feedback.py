"""Routing feedback uses the same current-authority transaction boundary."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, event, select, update

from app.core.security import decode_access_token
from app.db import authorization_http
from app.db.session import engine
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.routing_feedback import AiRoutingFeedback
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.user import User
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal


@pytest.mark.parametrize("role", ["user", "assistant", "tool", "system"])
async def test_feedback_http_uses_one_complete_plan(
    client, auth_token, seed_test_message, role
):
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            update(AiMessage)
            .where(AiMessage.message_id == seed_test_message)
            .values(role=role)
        )
    writes = []

    def capture(_conn, _cursor, sql, _params, context, _many):
        if sql.startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(
                context.execution_options.get("_authorization_plan_permit") is not None
            )

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        response = await client.post(
            f"/ai/messages/{seed_test_message}/routing-feedback",
            json={"feedback": "correct"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert response.status_code == 200, response.text
        assert len(writes) == 3 and all(writes), writes
        async with AsyncSessionLocal() as db:
            message = await db.get(AiMessage, seed_test_message)
            history = list(
                await db.scalars(
                    select(AiRoutingFeedback).where(
                        AiRoutingFeedback.message_id == seed_test_message
                    )
                )
            )
            assert message.routing_feedback == "correct"
            assert len(history) == 1 and history[0].feedback == "correct"
            logs = list(
                await db.scalars(
                    select(SysOperationLog).where(
                        SysOperationLog.path
                        == f"/ai/messages/{seed_test_message}/routing-feedback"
                    )
                )
            )
            assert len(logs) == 1 and logs[0].status_code == 200
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                delete(AiRoutingFeedback).where(
                    AiRoutingFeedback.message_id == seed_test_message
                )
            )
            await db.execute(
                delete(SysOperationLog).where(
                    SysOperationLog.path
                    == f"/ai/messages/{seed_test_message}/routing-feedback"
                )
            )


@pytest.mark.parametrize("change", ["version", "inactive", "deleted"])
async def test_feedback_rechecks_authority_after_request_handoff(
    client, auth_token, seed_test_message, monkeypatch, change
):
    original = authorization_http.release_request_reads
    user_id = int(decode_access_token(auth_token)["sub"])
    async with AsyncSessionLocal() as db:
        before = (await db.get(User, user_id)).auth_version

    async def release(db):
        await original(db)
        async with AsyncSessionLocal() as mutation, mutation.begin():
            message = await mutation.get(AiMessage, seed_test_message)
            if change == "version":
                user = await mutation.get(User, user_id)
                user.auth_version += 1
            elif change == "inactive":
                message.is_active = False
            else:
                conversation = await mutation.get(
                    AiConversation, message.conversation_id
                )
                conversation.deleted_at = datetime.now(UTC)

    monkeypatch.setattr(authorization_http, "release_request_reads", release)
    try:
        response = await client.post(
            f"/ai/messages/{seed_test_message}/routing-feedback",
            json={"feedback": "correct"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response.status_code in {401, 403, 404, 409}, response.text
        async with AsyncSessionLocal() as db:
            assert (await db.get(AiMessage, seed_test_message)).routing_feedback is None
            assert (
                await db.scalar(
                    select(AiRoutingFeedback.feedback_id).where(
                        AiRoutingFeedback.message_id == seed_test_message
                    )
                )
                is None
            )
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(
                update(User).where(User.user_id == user_id).values(auth_version=before)
            )
            await db.execute(
                delete(AiRoutingFeedback).where(
                    AiRoutingFeedback.message_id == seed_test_message
                )
            )
            await db.execute(
                delete(SysOperationLog).where(
                    SysOperationLog.path
                    == f"/ai/messages/{seed_test_message}/routing-feedback"
                )
            )
