"""Durable prepared-action startup recovery tests."""

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
        patch(
            "app.modules.ai.lifecycle.prepared_action_service.pending_source_is_valid",
            AsyncMock(return_value=True),
        ),
    ):
        cleaned = await cleanup_prepared_actions_on_startup(MagicMock())

    assert cleaned == 0
    transition.assert_not_awaited()


async def test_startup_keeps_running_action_with_live_execution_lease() -> None:
    """滚动发布时不能把仍由其它 Pod 执行的 action 标为失败。"""
    action = SimpleNamespace(
        action_id=9003,
        confirmation_id="cid_running_9003",
        status="running",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        execution_lease_expires_at=datetime.now(UTC) + timedelta(seconds=45),
        guard_owner_token="guard-live",
        conversation_id=102,
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


async def test_startup_expires_unexpired_pending_when_source_is_orphaned() -> None:
    action = SimpleNamespace(
        action_id=9002,
        confirmation_id="cid_orphaned_9002",
        status="pending_confirmation",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        guard_owner_token=None,
        conversation_id=101,
        source_user_message_id=102,
        user_id=103,
        tenant_id=0,
        execute_tool_call_id="tc_orphaned_9002",
        row_version=1,
    )
    scan_db = MagicMock()
    scan_result = MagicMock()
    scan_result.scalars.return_value.all.return_value = [action]
    scan_db.execute = AsyncMock(return_value=scan_result)

    cleanup_db = MagicMock()
    cleanup_db.begin = MagicMock()
    cleanup_db.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    cleanup_db.begin.return_value.__aexit__ = AsyncMock(return_value=None)

    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(
        side_effect=[scan_db, cleanup_db]
    )
    session_factory.return_value.__aexit__ = AsyncMock(return_value=None)
    transition = AsyncMock(return_value=action)

    with (
        patch("app.modules.ai.lifecycle.AsyncSessionLocal", session_factory),
        patch(
            "app.modules.ai.lifecycle.prepared_action_service.pending_source_is_valid",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.modules.ai.lifecycle.prepared_action_service.get_by_confirmation_id",
            AsyncMock(return_value=action),
        ),
        patch(
            "app.modules.ai.lifecycle.prepared_action_service.transition_status",
            transition,
        ),
        patch(
            "app.modules.ai.lifecycle.operation_log_service.get_by_tool_call_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.modules.ai.lifecycle.chat_run_finalizer.finalize_prepared_action",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.modules.ai.lifecycle.hitl_manager.delete_pending",
            AsyncMock(return_value=None),
        ),
    ):
        cleaned = await cleanup_prepared_actions_on_startup(MagicMock())

    assert cleaned == 1
    assert transition.await_args.kwargs["target_status"].value == "expired"
    assert (
        transition.await_args.kwargs["error_code"] == "AI_PREPARED_ACTION_SOURCE_STALE"
    )
