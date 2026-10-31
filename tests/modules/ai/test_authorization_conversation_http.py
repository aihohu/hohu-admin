"""Real conversation HTTP writes hand off before DNS and planned completion."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.exceptions import BusinessRuleException, setup_exception_handlers
from app.core.security import create_access_token
from app.db import authorization_chat_owner, authorization_http, session
from app.db.request_session import RequestSession
from app.modules.ai.api import conversation
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.models.conversation import AiConversation
from app.modules.auth import service
from app.modules.system.models.user import User
from tests.modules.ai.test_authorization_chat_lifecycle import (
    chat_model_case as chat_model_case,
)
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


@pytest.fixture
async def conversation_http(chat_model_case, monkeypatch):
    _, plain, case = chat_model_case
    engine = plain.kw["bind"]
    monkeypatch.setattr(authorization_http, "engine", engine)
    monkeypatch.setattr(
        session,
        "AsyncSessionLocal",
        async_sessionmaker(engine, class_=RequestSession, expire_on_commit=False),
    )
    monkeypatch.setattr(service, "_is_blacklisted", AsyncMock(return_value=False))
    monkeypatch.setattr(
        provider_egress, "is_model_allowed", AsyncMock(return_value=True)
    )
    app = FastAPI()
    setup_exception_handlers(app)
    app.include_router(conversation.router, prefix="/ai/conversation")
    token = create_access_token(
        subject="101", tenant_id=0, tenant_version=1, user_version=1
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client, plain, case


async def test_conversation_writes_have_g_first_one_ack_and_no_transaction_dns(
    conversation_http, monkeypatch
):
    client, plain, _ = conversation_http
    engine = plain.kw["bind"].sync_engine
    active, commits, locks, dns = set(), [], [], []

    def begin(connection):
        active.add(connection)

    def rollback(connection):
        active.discard(connection)

    def commit(connection):
        active.discard(connection)
        commits.append(connection)

    def sql(_connection, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    async def allowed(*_args, **_kwargs):
        assert not active, "Provider DNS must not retain the request transaction"
        dns.append(True)
        return True

    monkeypatch.setattr(provider_egress, "is_model_allowed", allowed)
    listeners = [
        ("begin", begin),
        ("rollback", rollback),
        ("commit", commit),
        ("before_cursor_execute", sql),
    ]
    for name, fn in listeners:
        event.listen(engine, name, fn)
    try:
        response = await client.post(
            "/ai/conversation", json={"title": "HTTP chat", "modelName": "7102"}
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert isinstance(data["conversationId"], str) and data["modelName"] == "7102"
        assert data["createTime"] and dns and len(commits) == 1
        assert "sys_user_field_policy_global_guard" in locks[0]
        identifier = data["conversationId"]
        for method, body in (("put", {"title": "Renamed"}), ("delete", None)):
            locks.clear()
            commits.clear()
            response = await client.request(
                method, f"/ai/conversation/{identifier}", json=body
            )
            assert response.status_code == 200, response.text
            assert (
                len(commits) == 1 and "sys_user_field_policy_global_guard" in locks[0]
            )
    finally:
        for name, fn in listeners:
            event.remove(engine, name, fn)


async def test_conversation_create_rechecks_identity_after_external_io(
    conversation_http, monkeypatch
):
    client, plain, _ = conversation_http

    async def revoke(*_args, **_kwargs):
        async with plain() as db:
            await db.execute(
                update(User).where(User.user_id == 101).values(auth_version=2)
            )
            await db.commit()
        return True

    monkeypatch.setattr(provider_egress, "is_model_allowed", revoke)
    response = await client.post(
        "/ai/conversation", json={"title": "Late denied", "modelName": "7102"}
    )
    assert response.status_code == 401, response.text
    async with plain() as db:
        assert (
            await db.scalar(
                select(AiConversation.conversation_id).where(
                    AiConversation.title == "Late denied"
                )
            )
            is None
        )


async def test_conversation_unknown_ack_does_not_replay(conversation_http, monkeypatch):
    client, plain, _ = conversation_http
    original = authorization_chat_owner.commit_authorization_session
    calls = []

    async def unknown(db):
        await original(db)
        calls.append(True)
        raise BusinessRuleException(
            "Unknown commit", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )

    monkeypatch.setattr(
        authorization_chat_owner, "commit_authorization_session", unknown
    )
    response = await client.post(
        "/ai/conversation", json={"title": "Unknown ack", "modelName": "7102"}
    )
    assert response.json().get("errorCode") == "AUTHORIZATION_COMMIT_UNKNOWN"
    assert len(calls) == 1
    async with plain() as db:
        assert (
            len(
                (
                    await db.scalars(
                        select(AiConversation.conversation_id).where(
                            AiConversation.title == "Unknown ack"
                        )
                    )
                ).all()
            )
            == 1
        )
