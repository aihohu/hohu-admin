"""HTTP deletion includes historical context in the complete lock plan."""

import pytest
from sqlalchemy import delete, event, insert, select

from app.db import authorization_http
from app.db.base import user_roles
from app.modules.ai.models.conversation import AiConversation
from app.modules.marketplace import models as marketplace_models
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_system_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    system_sessions as system_sessions,
)


@pytest.mark.parametrize("batch", [False, True])
async def test_http_delete_plans_context_before_user(http_system, batch):
    client, plain = http_system
    # Import the actual RESTRICT/SET NULL legacy dependency models before the
    # isolated schema is populated, matching the migration-created database.
    assert marketplace_models.App.__tablename__ == "mk_app"
    async with plain() as setup:
        await setup.execute(
            insert(User).values(
                tenant_id=0,
                user_id=501,
                user_name="deletable",
                status="1",
                hashed_password="unused",
            )
        )
        await setup.execute(
            insert(AiConversation).values(
                tenant_id=0, conversation_id=601, user_id=501, title="Historical"
            )
        )
        await setup.commit()
    sql = []

    def record(_c, _cursor, statement, _params, _context, _many):
        sql.append(statement)

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
    try:
        response = await client.request(
            "POST" if batch else "DELETE",
            "/user/batch-delete" if batch else "/user/501",
            json=[501] if batch else None,
        )
    finally:
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    locks = [s for s in sql if "FOR UPDATE" in s or "FOR SHARE" in s]
    assert "sys_user_field_policy_global_guard" in locks[0]
    context_lock = next(i for i, s in enumerate(locks) if "ai_conversation" in s)
    user_lock = next(i for i, s in enumerate(locks) if "FROM sys_user " in s)
    assert context_lock < user_lock
    async with plain() as observer:
        assert await observer.get(User, 501) is None
        assert await observer.get(AiConversation, 601) is None
        assert (
            await observer.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 2
        )


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("changes", [1, 2])
async def test_late_context_is_discovered_without_replaying_delete(
    http_system, monkeypatch, batch, changes
):
    client, plain = http_system
    async with plain() as setup:
        await setup.execute(
            insert(User).values(
                tenant_id=0,
                user_id=501,
                user_name="deletable",
                status="1",
                hashed_password="unused",
            )
        )
        await setup.commit()
    original = authorization_http.release_request_reads
    calls = []

    async def changed(db):
        await original(db)
        calls.append(1)
        async with plain() as writer:
            for index in range(changes):
                await writer.execute(
                    insert(AiConversation).values(
                        tenant_id=0,
                        conversation_id=601 + index,
                        user_id=501,
                        title="Late context",
                    )
                )
            await writer.commit()

    monkeypatch.setattr(authorization_http, "release_request_reads", changed)
    response = await client.request(
        "POST" if batch else "DELETE",
        "/user/batch-delete" if batch else "/user/501",
        json=[501] if batch else None,
    )
    assert response.status_code == 200, response.text
    assert calls == [1]
    async with plain() as observer:
        assert await observer.get(User, 501) is None
        assert not list(
            await observer.scalars(
                select(AiConversation).where(AiConversation.user_id == 501)
            )
        )


@pytest.mark.parametrize("batch", [False, True])
async def test_delete_rechecks_revoked_permission_after_auth(
    http_system, monkeypatch, batch
):
    client, plain = http_system
    async with plain() as setup:
        await setup.execute(
            insert(User).values(
                tenant_id=0,
                user_id=501,
                user_name="deletable",
                status="1",
                hashed_password="unused",
            )
        )
        await setup.execute(
            insert(AiConversation).values(
                tenant_id=0, conversation_id=601, user_id=501, title="Preserved"
            )
        )
        await setup.commit()
    original = authorization_http.release_request_reads
    calls = []

    async def revoked(db):
        await original(db)
        calls.append(1)
        async with plain() as writer:
            await writer.execute(
                delete(user_roles).where(
                    user_roles.c.user_id == 101, user_roles.c.tenant_id == 0
                )
            )
            await writer.commit()

    monkeypatch.setattr(authorization_http, "release_request_reads", revoked)
    response = await client.request(
        "POST" if batch else "DELETE",
        "/user/batch-delete" if batch else "/user/501",
        json=[501] if batch else None,
    )
    assert response.status_code == 403, response.text
    assert calls == [1]
    async with plain() as observer:
        assert await observer.get(User, 501) is not None
        assert await observer.get(AiConversation, 601) is not None
        assert (
            await observer.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 1
        )
