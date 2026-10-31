"""Tenant control uses fresh planned transactions at the real HTTP boundary."""

import asyncio
import json

import pytest
from sqlalchemy import event, select

from app.core.config import settings
from app.modules.platform import api
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user_field_policy import UserFieldPolicyGlobalGuard
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


@pytest.fixture
async def tenant_http(catalog_http, monkeypatch):
    client, plain = catalog_http
    client._transport.app.include_router(api.control_router, prefix="/platform")
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    yield client, plain


async def test_prepare_concurrent_same_key_allocates_once_under_global_lock(
    tenant_http,
):
    client, plain = tenant_http
    locks = []
    engine = plain.kw["bind"].sync_engine

    def sql(_c, _cursor, statement, _params, _context, _many):
        if (
            "FOR UPDATE" in statement
            or "FOR SHARE" in statement
            or "advisory" in statement
        ):
            locks.append(statement)

    event.listen(engine, "before_cursor_execute", sql)
    try:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/platform/tenants",
                    json={"tenantCode": "new-http", "tenantName": "New HTTP"},
                    headers={"Idempotency-Key": "http-tenant-same-key"},
                )
                for _ in range(2)
            )
        )
    finally:
        event.remove(engine, "before_cursor_execute", sql)
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    ids = {r.json()["data"]["tenantId"] for r in responses}
    assert len(ids) == 1 and isinstance(next(iter(ids)), str)
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    assert not any("advisory" in sql for sql in locks)
    assert responses[0].json()["data"]["bootstrapStatus"] == "pending"
    assert responses[0].json()["data"]["createdAt"].endswith("Z")
    async with plain() as db:
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 2
        assert (
            len(
                (
                    await db.execute(
                        select(Tenant).where(Tenant.tenant_code == "new-http")
                    )
                )
                .scalars()
                .all()
            )
            == 1
        )
        logs = (await db.execute(select(SysOperationLog))).scalars().all()
        assert len(logs) == 4
        for row in logs:
            values = json.loads(row.request_params)
            assert values["target_tenant_id"] is None
            if row.action == "completed":
                assert values["result_summary"]["tenantId"] in ids
    conflict = await client.post(
        "/platform/tenants",
        json={"tenantCode": "different", "tenantName": "Different"},
        headers={"Idempotency-Key": "http-tenant-same-key"},
    )
    assert conflict.status_code == 409, conflict.text


async def test_prepare_bootstrap_activate_disable_http(tenant_http):
    client, plain = tenant_http
    created = await client.post(
        "/platform/tenants",
        json={"tenantCode": "full-http", "tenantName": "Full HTTP"},
        headers={"Idempotency-Key": "http-tenant-full-key"},
    )
    assert created.status_code == 200, created.text
    tid = created.json()["data"]["tenantId"]
    bootstrap = await client.post(
        f"/platform/tenants/{tid}/bootstrap",
        json={"defaultModelId": "601", "adminPassword": "Strong123!"},
        headers={"Idempotency-Key": "http-bootstrap-full-key"},
    )
    assert bootstrap.status_code == 200, bootstrap.text
    assert "Strong123!" not in bootstrap.text
    for action, enabled in (("activate", True), ("disable", False)):
        response = await client.post(f"/platform/tenants/{tid}/{action}")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["enabled"] is enabled
        assert response.json()["data"]["bootstrapStatus"] == "ready"
    async with plain() as db:
        logs = (await db.execute(select(SysOperationLog))).scalars().all()
        assert len(logs) == 8
        assert all("Strong123!" not in row.request_params for row in logs)
