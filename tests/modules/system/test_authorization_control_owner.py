"""Intent and completion have separate owners but one immutable request identity."""

import pytest
from sqlalchemy import select

from app.core.authorization_plan import LockPlan, TenantLock
from app.core.exceptions import BusinessException
from app.db.authorization_control_owner import control_candidate_owner
from app.modules.ai.core.provider_egress import provider_egress
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.schemas.provider import ProviderCreate
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.service.authorization_control_audit import ControlAudit
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def authorize(factory, **changes):
    request = {
        "tenant": TENANT,
        "actor_user_version": 1,
        "operation": "provider.create",
        "permission": "platform:ai:write",
        "reason": "Create test provider",
        "ticket_id": "test-2b3",
        "correlation_id": "control-owner-test",
        "target_tenant_id": None,
        "path": "/platform/ai/providers",
        "method": "POST",
    }
    request.update(changes)
    async with factory() as db:
        audit = await control_candidate_owner.authorize(db, **request)
        assert not db.in_transaction()
        return audit


async def test_independent_intent_survives_business_rollback(system_sessions):
    factory, plain = system_sessions
    audit = await authorize(factory)
    async with factory() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            await tx.lock(LockPlan(existing=audit.existing, new=audit.new))
            await audit.append(tx, result_summary={"created": 1})
        await db.rollback()
    async with plain() as db:
        rows = (
            (
                await db.execute(
                    select(SysOperationLog).order_by(SysOperationLog.operation_log_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].action == "authorized"
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 1
        )
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"actor_user_version": 2},
        {"operation": "provider.delete"},
        {"permission": "platform:tenant:write"},
        {"target_tenant_id": 8},
    ],
)
async def test_intent_cannot_be_reused_for_a_different_request(
    system_sessions, changes
):
    factory, _ = system_sessions
    audit = await authorize(factory)
    request = {
        "tenant": TENANT,
        "actor_user_version": 1,
        "operation": "provider.create",
        "permission": "platform:ai:write",
        "target_tenant_id": None,
    }
    request.update(changes)
    with pytest.raises(BusinessException):
        audit.assert_request(**request)


async def test_success_completion_does_not_change_authorization_epoch(system_sessions):
    factory, plain = system_sessions
    audit = await authorize(factory)
    async with factory() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            await tx.lock(LockPlan(existing=audit.existing, new=audit.new))
            await audit.append(tx)
        await db.commit()
    async with plain() as db:
        assert (
            await db.execute(
                select(SysOperationLog.action).order_by(
                    SysOperationLog.operation_log_id
                )
            )
        ).scalars().all() == ["authorized", "completed"]
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 1


async def test_unified_owner_owns_intent_business_and_completion(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions
    async with factory() as db:

        async def validate(*_args, **_kwargs):
            assert not db.in_transaction()

        monkeypatch.setattr(provider_egress, "validate_destination", validate)
        result = await control_candidate_owner.execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            operation="provider.create",
            payload=ProviderCreate(
                provider_code="openai", name="Owner test", api_key="test-owner-secret"
            ),
            audit_context={
                "reason": "test",
                "ticket_id": "T-1",
                "correlation_id": "owner-create",
            },
            audit_factory=factory,
        )
        assert not db.in_transaction()
    async with plain() as db:
        assert await db.scalar(select(AiProvider.provider_id)) is not None
        assert (
            await db.execute(
                select(SysOperationLog.action).order_by(
                    SysOperationLog.operation_log_id
                )
            )
        ).scalars().all() == ["authorized", "completed"]
        assert result is not None


async def test_owner_rolls_back_business_when_success_audit_fails_and_records_failure(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions

    async def fail_completion(*_args, **_kwargs):
        raise BusinessException(
            503, "audit unavailable", error_code="AUDIT_TEST_FAILURE"
        )

    async def dns(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ControlAudit, "append", fail_completion)
    monkeypatch.setattr(provider_egress, "validate_destination", dns)
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await control_candidate_owner.execute(
                db,
                tenant=TENANT,
                actor_user_version=1,
                operation="provider.create",
                payload=ProviderCreate(
                    provider_code="openai", name="must rollback", api_key="test-secret"
                ),
                audit_context={
                    "reason": "test",
                    "ticket_id": "T-2",
                    "correlation_id": "failed-success-audit",
                },
                audit_factory=factory,
            )
        assert error.value.error_code == "AUDIT_TEST_FAILURE"
        assert not db.in_transaction()
    async with plain() as db:
        assert await db.scalar(select(AiProvider.provider_id)) is None
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 1
        rows = (
            await db.execute(
                select(SysOperationLog.action, SysOperationLog.status_code).order_by(
                    SysOperationLog.operation_log_id
                )
            )
        ).all()
        # An intent has no outcome yet; only the completion records HTTP status.
        assert rows == [("authorized", None), ("completed", 503)]
