"""Prepared tenant bootstrap composes System and AI under one complete plan."""

import pytest
from sqlalchemy import insert, select

from app.core.exceptions import BusinessException
from app.modules.ai.models.model import AiModel
from app.modules.ai.models.model_policy import TenantAiModelPolicy
from app.modules.ai.models.provider import AiProvider
from app.modules.platform.authorization_bootstrap_candidate import (
    tenant_bootstrap_candidate,
)
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from tests.modules.system.test_authorization_control_owner import authorize
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_tenant_candidate import run
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def prepare(factory, plain):
    await run(
        factory,
        "tenant.prepare",
        50,
        {
            "tenant_code": "bootstrap-new",
            "tenant_name": "Bootstrap",
            "idempotency_key": "tenant-bootstrap-prepare-01",
        },
    )
    async with plain() as db:
        await db.execute(
            insert(AiProvider).values(
                provider_id=100,
                provider_code="bootstrap-provider",
                name="Test",
                api_key="ciphertext",
                is_enabled=True,
            )
        )
        await db.execute(
            insert(AiModel).values(
                model_id=101,
                provider_id=100,
                name="text",
                capabilities=["text"],
                is_enabled=True,
            )
        )
        await db.commit()


async def bootstrap(factory, **changes):
    payload = {
        "default_model_id": 101,
        "admin_password": "Bootstrap123!",
        "idempotency_key": "tenant-bootstrap-new-001",
    }
    payload.update(changes)
    audit = await authorize(
        factory,
        operation="tenant.bootstrap",
        permission="platform:tenant:bootstrap",
        target_tenant_id=50,
    )
    async with factory() as db:
        result = await tenant_bootstrap_candidate.execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            target_tenant_id=50,
            payload=payload,
            audit=audit,
        )
        await db.commit()
        return result


async def test_bootstrap_commits_one_target_epoch_and_replays(system_sessions):
    factory, plain = system_sessions
    await prepare(factory, plain)
    first = await bootstrap(factory)
    replay = await bootstrap(factory)
    assert first.replayed is False and replay.replayed is True
    assert first.role_count == 2 and first.menu_count > 0
    async with plain() as db:
        assert (
            await db.scalar(
                select(User.user_id).where(
                    User.tenant_id == 50, User.user_name == "admin"
                )
            )
            is not None
        )
        assert (
            await db.scalar(
                select(TenantAiModelPolicy.is_default).where(
                    TenantAiModelPolicy.tenant_id == 50
                )
            )
            is True
        )
        assert await db.scalar(select(UserFieldPolicyGlobalGuard.epoch)) == 2
        assert (
            await db.execute(
                select(
                    UserFieldPolicyState.tenant_id, UserFieldPolicyState.epoch
                ).order_by(UserFieldPolicyState.tenant_id)
            )
        ).all() == [(0, 1), (1, 1), (50, 2)]


async def test_bootstrap_model_failure_rolls_back_all_target_rows(system_sessions):
    factory, plain = system_sessions
    await prepare(factory, plain)
    with pytest.raises(BusinessException) as error:
        await bootstrap(factory, default_model_id=999)
    assert error.value.error_code == "PLATFORM_TENANT_BOOTSTRAP_MODEL_UNAVAILABLE"
    async with plain() as db:
        assert await db.scalar(select(User.user_id).where(User.tenant_id == 50)) is None
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 50
                )
            )
            == 1
        )
