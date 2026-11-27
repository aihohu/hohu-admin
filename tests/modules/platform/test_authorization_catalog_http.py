"""Platform catalog HTTP contract against independent committed connections."""

import json

import pytest
from sqlalchemy import event, insert, select, update

from app.modules.ai.models.provider import AiProvider
from app.modules.ai.service import authorization_catalog_probe
from app.modules.platform import ai_api, system_agent_auth
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.test_authorization_catalog_candidate import (
    batch_payload,
)
from tests.test_authorization_catalog_candidate import (
    catalog_sessions as catalog_sessions,
)


@pytest.fixture
async def catalog_http(http_system, catalog_sessions, monkeypatch):
    client, plain = http_system
    assert catalog_sessions[1] is plain
    client._transport.app.include_router(ai_api.router, prefix="/platform")
    # The red baseline's separate legacy intent writes must stay in this schema.
    monkeypatch.setattr(system_agent_auth, "AsyncSessionLocal", plain)
    client.headers.update(
        {
            "X-Platform-Reason": "Verify real HTTP contract",
            "X-Platform-Ticket": "HTTP-2B5",
            "X-Correlation-ID": "http-catalog",
        }
    )
    async with plain() as db:
        await db.execute(
            insert(AiProvider).values(
                provider_id=505,
                provider_code="anthropic",
                name="Empty",
                api_key="",
                config={},
                is_enabled=False,
            )
        )
        await db.commit()
    yield client, plain


@pytest.mark.parametrize(
    "method,path,payload,id_key",
    [
        ("PUT", "/platform/ai/agents/701", {"name": "Updated"}, "agentId"),
        (
            "POST",
            "/platform/ai/providers",
            {
                "providerCode": "deepseek",
                "name": "New",
                "apiKey": "private-test-key",
                "baseUrl": "https://api.deepseek.com/v1",
            },
            "providerId",
        ),
        ("PUT", "/platform/ai/providers/501", {"name": "Updated"}, "providerId"),
        ("DELETE", "/platform/ai/providers/505", None, None),
        (
            "POST",
            "/platform/ai/providers/501/models",
            {"name": "model-three", "capabilities": ["text"]},
            "modelId",
        ),
        (
            "PUT",
            "/platform/ai/providers/501/models/601",
            {"name": "renamed"},
            "modelId",
        ),
        ("DELETE", "/platform/ai/providers/501/models/602", None, None),
    ],
)
async def test_catalog_http_complete_plan_audit_and_response(
    catalog_http, method, path, payload, id_key
):
    client, plain = catalog_http
    commits, locks = [], []
    engine = plain.kw["bind"].sync_engine

    def commit(connection):
        commits.append(connection)

    def sql(_connection, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    event.listen(engine, "commit", commit)
    event.listen(engine, "before_cursor_execute", sql)
    try:
        response = await client.request(method, path, json=payload)
    finally:
        event.remove(engine, "commit", commit)
        event.remove(engine, "before_cursor_execute", sql)
    assert response.status_code == 200, response.text
    assert len(commits) == 2, "Intent and business completion each commit exactly once"
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    assert any(
        "sys_user_field_policy_global_guard" in sql and "FOR UPDATE" in sql
        for sql in locks
    )
    assert "private-test-key" not in response.text
    if id_key:
        assert isinstance(response.json()["data"][id_key], str)
    async with plain() as db:
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 2
        rows = (
            (
                await db.execute(
                    select(SysOperationLog).order_by(SysOperationLog.operation_log_id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.action for row in rows] == ["authorized", "completed"]
        assert all(
            row.method == method
            and row.path.startswith("/platform/")
            and "authorization-candidates" not in row.path
            for row in rows
        )
        assert (
            json.loads(rows[-1].request_params)["authorization_audit_id"]
            == rows[0].operation_log_id
        )


async def test_catalog_missing_context_has_planned_denial(catalog_http):
    client, plain = catalog_http
    del client.headers["X-Platform-Reason"]
    locks = []
    engine = plain.kw["bind"].sync_engine

    def sql(_connection, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    event.listen(engine, "before_cursor_execute", sql)
    try:
        response = await client.put(
            "/platform/ai/agents/701", json={"name": "Forbidden"}
        )
    finally:
        event.remove(engine, "before_cursor_execute", sql)
    assert response.status_code == 400, response.text
    assert response.json()["errorCode"] == "PLATFORM_AUDIT_CONTEXT_REQUIRED"
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    async with plain() as db:
        rows = (await db.execute(select(SysOperationLog))).scalars().all()
        assert len(rows) == 1 and rows[0].action == "denied"
        assert "PLATFORM_AUDIT_CONTEXT_REQUIRED" in rows[0].request_params


async def test_model_policy_http_single_batch_delete(catalog_http):
    client, plain = catalog_http
    path = "/platform/tenants/1/ai/model-policies"
    single = await client.put(path + "/601", json={"enabled": True, "isDefault": True})
    assert single.status_code == 200, single.text
    assert single.json()["data"]["modelId"] == "601"
    payload = await batch_payload(plain, enabled=True)
    batch = await client.put(path, json=payload.model_dump(mode="json", by_alias=True))
    assert batch.status_code == 200, batch.text
    assert len(batch.json()["data"]["models"]) == 2
    deleted = await client.delete(path + "/601")
    assert deleted.status_code == 200, deleted.text
    async with plain() as db:
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 1
        epochs = dict(
            (
                await db.execute(
                    select(UserFieldPolicyState.tenant_id, UserFieldPolicyState.epoch)
                )
            ).all()
        )
        assert epochs == {0: 1, 1: 4}
        logs = (await db.execute(select(SysOperationLog))).scalars().all()
        assert len(logs) == 6


@pytest.mark.parametrize("draft", [False, True])
@pytest.mark.parametrize("revoke", [False, True])
async def test_probe_http_releases_transaction_and_rechecks_identity(
    catalog_http, monkeypatch, draft, revoke
):
    client, plain = catalog_http
    active, calls = set(), []
    engine = plain.kw["bind"].sync_engine

    def begin(connection):
        active.add(connection)

    def end(connection):
        active.discard(connection)

    async def probe(effective):
        assert not active, "Network probe cannot retain a database transaction"
        calls.append(effective["model"]["name"])
        if revoke:
            async with plain() as db:
                await db.execute(
                    update(User).where(User.user_id == 101).values(auth_version=2)
                )
                await db.commit()

    monkeypatch.setattr(authorization_catalog_probe, "probe", probe)
    for name, callback in (("begin", begin), ("commit", end), ("rollback", end)):
        event.listen(engine, name, callback)
    try:
        response = await client.post(
            "/platform/ai/providers/test"
            if draft
            else "/platform/ai/providers/501/test",
            json={
                "providerId": "501",
                "providerCode": "openai",
                "model": {"name": "draft", "capabilities": ["text"]},
            }
            if draft
            else {"modelId": "601"},
        )
    finally:
        for name, callback in (("begin", begin), ("commit", end), ("rollback", end)):
            event.remove(engine, name, callback)
    assert response.status_code == (401 if revoke else 200), response.text
    assert len(calls) == 1
    assert "private-test-key" not in response.text
    async with plain() as db:
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 1
        logs = (
            (
                await db.execute(
                    select(SysOperationLog).order_by(SysOperationLog.operation_log_id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.action for row in logs] == ["authorized", "completed"]
        assert logs[-1].status_code == (401 if revoke else 200)


async def test_validation_after_intent_records_one_failed_completion(catalog_http):
    client, plain = catalog_http
    response = await client.put("/platform/ai/providers/501", json={"name": []})
    assert response.status_code == 422, response.text
    async with plain() as db:
        logs = (
            (
                await db.execute(
                    select(SysOperationLog).order_by(SysOperationLog.operation_log_id)
                )
            )
            .scalars()
            .all()
        )
        assert [row.action for row in logs] == ["authorized", "completed"]
        assert logs[-1].status_code == 422
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 1


@pytest.mark.parametrize("identifier", ["-1", "0", "bad", str(2**63), "9" * 100])
async def test_invalid_catalog_path_retains_validation_status(catalog_http, identifier):
    client, plain = catalog_http
    response = await client.put(
        f"/platform/ai/agents/{identifier}", json={"name": "Invalid"}
    )
    assert response.status_code == 422, response.text
    async with plain() as db:
        assert not (await db.execute(select(SysOperationLog))).scalars().all()
