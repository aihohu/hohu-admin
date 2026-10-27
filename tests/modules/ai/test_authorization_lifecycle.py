"""Pending decisions and claim ownership use the complete transaction plan."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.core.exceptions import BusinessException
from app.db.authorization_ai_owner import AIAuthorizationOwner
from app.db.authorization_lease_owner import LeaseOwner
from app.db.authorization_lifecycle_owner import LifecycleOwner
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.system.models.dept import Dept
from app.modules.system.models.user import User
from tests.modules.ai.test_authorization_gateway_execution import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_recovery_candidate import (
    recovery_case as recovery_case,
)
from tests.modules.system.test_authorization_system_candidate import epoch
from tests.modules.system.test_authorization_system_candidate import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)


@pytest.fixture
async def pending_case(execution_case):
    factory, plain, case = execution_case
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                status="pending_confirmation",
                row_version=1,
                approved_by=None,
                approved_at=None,
                execution_owner=None,
                execution_lease_expires_at=None,
            )
        )
        await db.execute(
            update(AiOperationLog).values(
                status="pending_confirmation", approved_by=None, started_at=None
            )
        )
        await db.commit()
    return factory, plain, case


async def test_confirm_executes_once(pending_case):

    factory, plain, case = pending_case
    owner = LifecycleOwner(factory)
    assert (
        await owner.confirm(
            case.claim.confirmation_id, tenant=case.tenant, actor_user_version=1
        )
    ).disposition == "succeeded"
    assert (
        await owner.confirm(
            case.claim.confirmation_id, tenant=case.tenant, actor_user_version=1
        )
    ).disposition == "succeeded"
    async with plain() as db:
        assert len(list(await db.scalars(select(Dept)))) == 1
        assert await epoch(db) == 2


@pytest.mark.parametrize(
    "decision,expected", [("reject", "rejected"), ("cancel", "expired")]
)
async def test_unexecuted_decisions_keep_epoch(pending_case, decision, expected):

    factory, plain, case = pending_case
    result = await LifecycleOwner(factory).decide(
        case.claim.confirmation_id,
        tenant=case.tenant,
        actor_user_version=1,
        decision=decision,
    )
    assert result == expected
    async with plain() as db:
        action = await db.scalar(select(AiPreparedAction))
        log = await db.scalar(select(AiOperationLog))
        assert action.status == log.status == expected
        assert action.execution_owner is None and await epoch(db) == 1


async def test_revoked_user_cannot_claim(pending_case):

    factory, plain, case = pending_case
    async with plain() as db:
        await db.execute(update(User).where(User.user_id == 101).values(auth_version=2))
        await db.commit()
    with pytest.raises(BusinessException):
        await LifecycleOwner(factory).confirm(
            case.claim.confirmation_id, tenant=case.tenant, actor_user_version=1
        )
    async with plain() as db:
        assert (
            await db.scalar(select(AiPreparedAction.status))
        ) == "pending_confirmation"


async def test_fact_expiry_survives_user_revocation(pending_case):

    factory, plain, case = pending_case
    async with plain() as db:
        await db.execute(update(User).where(User.user_id == 101).values(status="2"))
        await db.execute(
            update(AiPreparedAction).values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db.commit()
    page = await AIAuthorizationOwner(factory).expire_page(limit=1)
    assert len(page.items) == 1 and page.items[0].disposition == "expired"
    async with plain() as db:
        assert await epoch(db) == 1


async def test_lease_owner_only_changes_matching_live_claim(execution_case):

    factory, plain, case = execution_case
    owner = LeaseOwner(factory)
    assert await owner.renew(case.claim, seconds=90)
    assert not await owner.renew(replace(case.claim, row_version=999), seconds=90)
    async with plain() as db:
        action = await db.scalar(select(AiPreparedAction))
        assert (
            action.status == "running" and action.row_version == case.claim.row_version
        )
        assert (
            action.execution_owner == case.claim.execution_owner
            and await epoch(db) == 1
        )


async def test_concurrent_confirmation_cannot_execute_twice(pending_case):
    factory, plain, case = pending_case
    owner = LifecycleOwner(factory)
    results = await asyncio.gather(
        *(
            owner.confirm(
                case.claim.confirmation_id, tenant=case.tenant, actor_user_version=1
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert any(getattr(value, "disposition", None) == "succeeded" for value in results)
    async with plain() as db:
        assert len(list(await db.scalars(select(Dept)))) == 1 and await epoch(db) == 2


async def test_expired_lease_cannot_be_revived(execution_case):
    factory, plain, case = execution_case
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                execution_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db.commit()
    assert not await LeaseOwner(factory).renew(case.claim)
    async with plain() as db:
        assert (await db.scalar(select(AiPreparedAction))).status == "running"
