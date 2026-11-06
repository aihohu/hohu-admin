"""Audited GETs release transactions for DNS and recheck authority afterwards."""

import json

import pytest
from sqlalchemy import event, select, update

from app.core.exceptions import BusinessException
from app.db import authorization_catalog_read_owner as owner
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.models.provider import AiProvider
from app.modules.platform import api
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.user import User
from tests.modules.platform.test_authorization_catalog_http import (
    catalog_http as catalog_http,
)
from tests.modules.platform.test_authorization_catalog_http import (
    catalog_sessions as catalog_sessions,
)
from tests.modules.platform.test_authorization_catalog_http import (
    http_system as http_system,
)
from tests.modules.platform.test_authorization_catalog_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.platform.test_authorization_catalog_http import (
    system_sessions as system_sessions,
)


@pytest.mark.parametrize(
    "path",
    [
        "/platform/ai/agents",
        "/platform/ai/agents/701",
        "/platform/ai/agents/model-options",
        "/platform/ai/providers",
        "/platform/ai/providers/models",
        "/platform/ai/providers/501/models",
        "/platform/tenants/0/ai/model-policies",
        "/platform/tenants/0/ai/model-policies/catalog",
        "/platform/tenants",
        "/platform/tenants/0",
    ],
)
async def test_catalog_gets_plan_audit_and_release_dns(catalog_http, monkeypatch, path):
    client, plain = catalog_http
    client._transport.app.include_router(api.control_router, prefix="/platform")
    engine = plain.kw["bind"].sync_engine
    active, commits, locks = set(), [], []

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
        assert not active, (
            "Catalog DNS must not retain any request or owner transaction"
        )
        return True

    monkeypatch.setattr(provider_egress, "is_configuration_allowed", allowed)
    listeners = [
        ("begin", begin),
        ("rollback", rollback),
        ("commit", commit),
        ("before_cursor_execute", sql),
    ]
    for name, fn in listeners:
        event.listen(engine, name, fn)
    try:
        response = await client.get(path)
    finally:
        for name, fn in listeners:
            event.remove(engine, name, fn)
    assert response.status_code == 200, response.text
    assert len(commits) == 2
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    async with plain() as db:
        logs = (
            await db.scalars(
                select(SysOperationLog)
                .where(SysOperationLog.audit_scope == "platform")
                .order_by(SysOperationLog.operation_log_id)
            )
        ).all()
        assert [row.action for row in logs] == ["authorized", "completed"]
        detail = json.loads(logs[-1].request_params)
        assert detail["authorization_audit_id"] == logs[0].operation_log_id


async def test_catalog_dns_cannot_project_after_actor_revocation(
    catalog_http, monkeypatch
):
    client, plain = catalog_http

    async def revoke(*_args, **_kwargs):
        async with plain() as db:
            await db.execute(
                update(User).where(User.user_id == 101).values(auth_version=2)
            )
            await db.commit()
        return True

    monkeypatch.setattr(provider_egress, "is_configuration_allowed", revoke)
    response = await client.get("/platform/ai/providers")
    assert response.status_code == 401, response.text


async def test_catalog_dns_rejects_changed_configuration(catalog_http, monkeypatch):
    client, plain = catalog_http

    async def change(*_args, **_kwargs):
        async with plain() as db:
            await db.execute(
                update(AiProvider)
                .where(AiProvider.provider_id == 501)
                .values(base_url="https://changed.example/v1")
            )
            await db.commit()
        return True

    monkeypatch.setattr(provider_egress, "is_configuration_allowed", change)
    response = await client.get("/platform/ai/providers")
    assert response.status_code == 409, response.text
    assert response.json()["errorCode"] == "AUTHORIZATION_SNAPSHOT_STALE"


async def test_catalog_unknown_completion_does_not_append_opposing_audit(
    catalog_http, monkeypatch
):
    client, plain = catalog_http
    original = owner.commit_authorization_session

    async def unknown(db):
        await original(db)
        raise BusinessException(
            503, "Unknown ACK", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )

    monkeypatch.setattr(owner, "commit_authorization_session", unknown)
    response = await client.get("/platform/ai/agents")
    assert response.status_code == 503, response.text
    async with plain() as db:
        rows = (
            await db.scalars(
                select(SysOperationLog)
                .where(SysOperationLog.audit_scope == "platform")
                .order_by(SysOperationLog.operation_log_id)
            )
        ).all()
        assert [row.action for row in rows] == ["authorized", "completed"]
        assert rows[-1].status_code == 200


async def test_catalog_missing_agent_records_failed_completion(catalog_http):
    client, plain = catalog_http
    response = await client.get("/platform/ai/agents/99999")
    assert response.status_code == 404, response.text
    async with plain() as db:
        rows = (
            await db.scalars(
                select(SysOperationLog)
                .where(SysOperationLog.audit_scope == "platform")
                .order_by(SysOperationLog.operation_log_id)
            )
        ).all()
        assert [row.action for row in rows] == ["authorized", "completed"]
        assert rows[-1].status_code == 404
