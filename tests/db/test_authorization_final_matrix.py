"""Missing cross-owner races use real PostgreSQL blockers in both orders."""

import asyncio

import pytest
from sqlalchemy import insert, select, update

from app.core.config import settings
from app.core.security import create_access_token
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.base import user_roles
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.schemas.model import ModelUpdate
from app.modules.platform import api as platform_api
from app.modules.system.models.dept import Dept
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyGlobalGuard
from tests.db.test_authorization_runtime_concurrency import CommitGate
from tests.modules.ai.test_authorization_gateway_execution import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_recovery_candidate import (
    recovery_case as recovery_case,
)
from tests.modules.system.test_authorization_system_candidate import TENANT, epoch
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
    catalog_sessions as catalog_sessions,
)

AUDIT = {
    "reason": "Verify serialization",
    "ticket_id": "matrix",
    "correlation_id": "matrix",
}


async def race(monkeypatch, plain, first, second, pause_sql):
    gate = CommitGate(monkeypatch, plain.kw["bind"], pause_sql=pause_sql)
    tasks = []
    try:
        tasks.append(asyncio.create_task(first(), name="first-owner"))
        await asyncio.wait_for(gate.entered.wait(), 1.5)
        tasks.append(asyncio.create_task(second(), name="second-owner"))
        await gate.assert_blocked(plain)
        assert not tasks[1].done()
        gate.release.set()
        return await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        gate.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("kind", ["menu", "model"])
@pytest.mark.parametrize("revoke_first", [False, True])
async def test_menu_model_revocation_against_ai_both_orders(
    execution_case, catalog_sessions, monkeypatch, kind, revoke_first
):
    _, plain, case = execution_case
    assert catalog_sessions[1] is plain
    runtime = AuthorizationRuntime(plain.kw["bind"])
    if kind == "model":
        async with plain() as db:
            await db.execute(
                insert(TenantAiModelPolicy).values(
                    tenant_id=0, model_id=601, enabled=True, is_default=True
                )
            )
            await db.execute(
                update(AiPreparedAction).values(
                    resolved_model_id=601, resolved_provider_id=501
                )
            )
            await db.commit()

    async def revoke():
        return await runtime.control(
            tenant=TENANT,
            actor_user_version=1,
            operation="menu.delete" if kind == "menu" else "model.update",
            target_id=9001 if kind == "menu" else 601,
            provider_id=501 if kind == "model" else None,
            payload=None if kind == "menu" else ModelUpdate(is_enabled=False),
            audit_context=None if kind == "menu" else AUDIT,
        )

    async def execute():
        return await runtime.gateway.execute_claim(
            case.claim, tenant=case.tenant, actor_user_version=1
        )

    results = await race(
        monkeypatch,
        plain,
        revoke if revoke_first else execute,
        execute if revoke_first else revoke,
        ("DELETE FROM sys_menu" if kind == "menu" else "UPDATE ai_model")
        if revoke_first
        else "INSERT INTO sys_dept",
    )
    outcome = results[1] if revoke_first else results[0]
    assert getattr(outcome, "disposition", None) == (
        "failed" if revoke_first else "succeeded"
    ), results
    assert not isinstance(results[0 if revoke_first else 1], BaseException), results
    async with plain() as db:
        assert len((await db.scalars(select(Dept))).all()) == int(not revoke_first)
        assert await epoch(db) == (2 if revoke_first else 3)


@pytest.mark.parametrize("revoke_first", [False, True])
async def test_tenant_disable_against_profile_both_orders(
    http_system, monkeypatch, revoke_first
):
    client, plain = http_system
    client._transport.app.include_router(
        platform_api.control_router, prefix="/platform"
    )
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    async with plain() as db:
        await db.execute(
            insert(user_roles).values(tenant_id=1, user_id=102, role_id=202)
        )
        await db.commit()
    token = create_access_token(
        subject="102", tenant_id=1, tenant_version=1, user_version=1
    )

    async def revoke():
        return await client.post(
            "/platform/tenants/1/disable",
            headers={
                "X-Platform-Reason": "Verify matrix",
                "X-Platform-Ticket": "matrix",
                "X-Correlation-ID": "matrix",
            },
        )

    async def write():
        return await client.put(
            "/user/profile",
            json={"nickname": "accepted"},
            headers={"Authorization": "Bearer " + token},
        )

    results = await race(
        monkeypatch,
        plain,
        revoke if revoke_first else write,
        write if revoke_first else revoke,
        "UPDATE sys_tenant" if revoke_first else "UPDATE sys_user ",
    )
    assert results[0].status_code == 200, results[0].text
    assert results[1].status_code == (401 if revoke_first else 200), results[1].text
    async with plain() as db:
        assert (
            await db.scalar(select(Tenant.status).where(Tenant.tenant_id == 1)) == "2"
        )
        assert await db.scalar(select(User.nickname).where(User.user_id == 102)) == (
            None if revoke_first else "accepted"
        )


@pytest.mark.parametrize("global_first", [False, True])
async def test_global_model_write_and_tenant_creation_both_orders(
    catalog_sessions, monkeypatch, global_first
):
    _, plain = catalog_sessions
    runtime = AuthorizationRuntime(plain.kw["bind"])

    async def global_write():
        return await runtime.control(
            tenant=TENANT,
            actor_user_version=1,
            operation="model.update",
            target_id=601,
            provider_id=501,
            payload=ModelUpdate(is_enabled=False),
            audit_context=AUDIT,
        )

    async def create():
        return await runtime.control(
            tenant=TENANT,
            actor_user_version=1,
            operation="tenant.prepare",
            target_tenant_id=55,
            payload={
                "tenant_code": "matrix-tenant",
                "tenant_name": "Matrix",
                "idempotency_key": "matrix-create-tenant-001",
            },
            audit_context=AUDIT,
        )

    results = await race(
        monkeypatch,
        plain,
        global_write if global_first else create,
        create if global_first else global_write,
        "UPDATE ai_model" if global_first else "INSERT INTO sys_tenant",
    )
    assert all(not isinstance(result, BaseException) for result in results), results
    async with plain() as db:
        assert (
            await db.scalar(select(AiModel.is_enabled).where(AiModel.model_id == 601))
            is False
        )
        assert (
            await db.scalar(select(Tenant.tenant_id).where(Tenant.tenant_id == 55))
            == 55
        )
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 3
