"""Tenant registry changes obey the structural and target barrier protocols."""

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.service.authorization_tenant_candidate import (
    tenant_control_candidate,
)
from tests.modules.platform.test_authorization_principal_candidate import migration
from tests.modules.system.test_authorization_control_owner import authorize
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def run(factory, operation, target, payload=None):
    audit = await authorize(
        factory,
        operation=operation,
        permission="platform:tenant:activate"
        if operation == "tenant.activate"
        else "platform:tenant:write",
        target_tenant_id=target,
    )
    async with factory() as db:
        result = await tenant_control_candidate.execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation=operation,
            target_tenant_id=target,
            payload=payload,
            audit=audit,
        )
        await db.commit()
        return result


async def test_prepare_creates_new_barrier_and_replay_changes_nothing(system_sessions):
    factory, plain = system_sessions
    payload = {
        "tenant_code": "new-customer",
        "tenant_name": "New Customer",
        "idempotency_key": "tenant-prepare-test-001",
    }
    first = await run(factory, "tenant.prepare", 50, payload)
    second = await run(factory, "tenant.prepare", 50, payload)
    assert first["tenant_id"] == second["tenant_id"] == 50
    assert first["replayed"] is False and second["replayed"] is True
    async with plain() as db:
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 2
        assert (
            await db.execute(
                select(
                    UserFieldPolicyState.tenant_id, UserFieldPolicyState.epoch
                ).order_by(UserFieldPolicyState.tenant_id)
            )
        ).all() == [(0, 1), (1, 1), (50, 1)]


async def test_disable_changes_target_once_and_retains_other_epochs(system_sessions):
    factory, plain = system_sessions
    await run(factory, "tenant.disable", 1)
    await run(factory, "tenant.disable", 1)
    async with plain() as db:
        row = await db.scalar(select(Tenant).where(Tenant.tenant_id == 1))
        assert (row.status, row.lifecycle_state) == ("2", "disabled")
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 1
        assert (
            await db.execute(
                select(
                    UserFieldPolicyState.tenant_id, UserFieldPolicyState.epoch
                ).order_by(UserFieldPolicyState.tenant_id)
            )
        ).all() == [(0, 1), (1, 2)]


async def test_prepare_conflict_does_not_create_second_tenant(system_sessions):
    factory, plain = system_sessions
    payload = {
        "tenant_code": "new-customer",
        "tenant_name": "New Customer",
        "idempotency_key": "tenant-prepare-test-001",
    }
    await run(factory, "tenant.prepare", 50, payload)
    with pytest.raises(BusinessException):
        await run(factory, "tenant.prepare", 51, payload)
    async with plain() as db:
        assert (
            await db.scalar(select(Tenant.tenant_id).where(Tenant.tenant_id == 51))
            is None
        )
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 2


async def test_default_tenant_cannot_be_disabled(system_sessions):
    factory, _ = system_sessions
    with pytest.raises(BusinessException) as error:
        await run(factory, "tenant.disable", 0)
    assert error.value.error_code == "PLATFORM_DEFAULT_TENANT_IMMUTABLE"


async def test_lifecycle_security_version_changes_once_and_activation_is_gated(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions
    async with plain() as db:
        await db.execute(migration["_CREATE_TENANT_VERSION_FUNCTION"])
        await db.execute(migration["_CREATE_TENANT_VERSION_TRIGGER"])
        await db.commit()
    await run(factory, "tenant.disable", 1)
    await run(factory, "tenant.disable", 1)
    monkeypatch.setattr(settings, "TENANT_MODE", "single")
    with pytest.raises(BusinessException) as error:
        await run(factory, "tenant.activate", 1)
    assert error.value.error_code == "PLATFORM_TENANT_ACTIVATION_DISABLED"
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    await run(factory, "tenant.activate", 1)
    await run(factory, "tenant.activate", 1)
    async with plain() as db:
        row = await db.scalar(select(Tenant).where(Tenant.tenant_id == 1))
        assert row.row_version == 3 and row.status == "1"
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 1
                )
            )
            == 3
        )
