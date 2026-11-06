"""Production startup recovery through real complete transaction plans."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.core.tenant import PlatformContext
from app.db.authorization_ai_owner import AIAuthorizationOwner
from app.modules.ai import lifecycle
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.authorization_lifecycle_candidate import lifecycle_candidate
from tests.modules.ai.test_authorization_background_recovery import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_background_recovery import (
    pending_case as pending_case,
)
from tests.modules.ai.test_authorization_background_recovery import (
    plan_sessions as plan_sessions,
)
from tests.modules.ai.test_authorization_background_recovery import (
    recovery_case as recovery_case,
)
from tests.modules.ai.test_authorization_background_recovery import (
    system_sessions as system_sessions,
)

PLATFORM = PlatformContext(
    0,
    "ai-lifecycle",
    "service",
    frozenset({"platform:ai:lifecycle"}),
    "startup recovery",
    "TEST",
    "startup",
)


@pytest.fixture
async def entry(pending_case, monkeypatch):
    factory, plain, case = pending_case
    runtime = SimpleNamespace(recovery=AIAuthorizationOwner(factory))
    monkeypatch.setattr(lifecycle, "get_authorization_runtime", lambda: runtime)
    monkeypatch.setattr(lifecycle, "_recovery_after", None)
    monkeypatch.setattr(lifecycle, "_recovery_startup_before", None)
    return plain, case


@pytest.mark.parametrize(
    "current_status", ["pending_confirmation", "approved", "running"]
)
async def test_periodic_expiry_rechecks_status_before_finalizing(
    entry, monkeypatch, current_status
):
    plain, _ = entry
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db.commit()
    original = lifecycle_candidate._locked

    async def claimed(*args, **kwargs):
        if current_status != "pending_confirmation":
            async with plain() as db:
                await db.execute(
                    update(AiPreparedAction).values(
                        status=current_status,
                        row_version=2 if current_status == "approved" else 3,
                    )
                )
                await db.commit()
        return await original(*args, **kwargs)

    monkeypatch.setattr(lifecycle_candidate, "_locked", claimed)
    cleaned = await lifecycle.cleanup_prepared_actions_on_startup(
        AsyncMock(), platform=PLATFORM, expired_only=True
    )
    assert cleaned == (1 if current_status == "pending_confirmation" else 0)
    async with plain() as db:
        assert await db.scalar(select(AiPreparedAction.status)) == (
            "expired" if cleaned else current_status
        )
        assert await db.scalar(select(AiOperationLog.status)) == (
            "expired" if cleaned else "pending_confirmation"
        )


async def test_startup_keeps_unexpired_pending_when_redis_was_flushed(entry):
    plain, _ = entry
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                expires_at=datetime.now(UTC) + timedelta(minutes=5)
            )
        )
        await db.commit()
    redis = AsyncMock()
    assert (
        await lifecycle.cleanup_prepared_actions_on_startup(redis, platform=PLATFORM)
        == 0
    )
    async with plain() as db:
        assert (
            await db.scalar(select(AiPreparedAction.status)) == "pending_confirmation"
        )
    redis.delete.assert_not_awaited()


async def test_startup_keeps_running_action_with_live_execution_lease(entry):
    plain, case = entry
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                status="running",
                row_version=3,
                approved_by=101,
                approved_at=datetime.now(UTC),
                execution_owner=case.claim.execution_owner,
                execution_lease_expires_at=datetime.now(UTC) + timedelta(seconds=45),
            )
        )
        await db.execute(
            update(AiOperationLog).values(
                status="running",
                approved_by=101,
                started_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        await db.commit()
    assert (
        await lifecycle.cleanup_prepared_actions_on_startup(
            AsyncMock(), platform=PLATFORM
        )
        == 0
    )
    async with plain() as db:
        assert await db.scalar(select(AiPreparedAction.status)) == "running"
        assert await db.scalar(select(AiOperationLog.status)) == "running"


async def test_startup_expires_unexpired_pending_when_source_is_orphaned(entry):
    plain, _ = entry
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                expires_at=datetime.now(UTC) + timedelta(minutes=5)
            )
        )
        await db.execute(update(AiMessage).values(is_active=False))
        await db.commit()
    assert (
        await lifecycle.cleanup_prepared_actions_on_startup(
            AsyncMock(), platform=PLATFORM
        )
        == 1
    )
    async with plain() as db:
        action = await db.scalar(select(AiPreparedAction))
        assert (
            action.status == "expired"
            and action.error_code == "AI_PREPARED_ACTION_SOURCE_STALE"
        )
        assert (
            await db.scalar(
                select(AiMessage.message_id).where(AiMessage.role == "assistant")
            )
            is None
        )
