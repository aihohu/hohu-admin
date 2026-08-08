"""Task 35a.5 durable prepared-action startup recovery."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.modules.ai.lifecycle import cleanup_prepared_actions_on_startup


async def test_startup_keeps_unexpired_pending_when_redis_was_flushed() -> None:
    action = SimpleNamespace(
        action_id=9001,
        confirmation_id="cid_durable_9001",
        status="pending_confirmation",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        guard_owner_token=None,
        conversation_id=100,
    )
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [action]
    db.execute = AsyncMock(return_value=result)
    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.modules.ai.lifecycle.AsyncSessionLocal", session_factory),
        patch(
            "app.modules.ai.lifecycle.prepared_action_service.transition_status",
            AsyncMock(),
        ) as transition,
    ):
        cleaned = await cleanup_prepared_actions_on_startup(MagicMock())

    assert cleaned == 0
    transition.assert_not_awaited()
