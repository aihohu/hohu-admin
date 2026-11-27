"""Legacy cache cleanup needs a complete, bound readonly database fact."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, event, insert, select, update

from app.core.tenant import PlatformContext
from app.db import authorization_ai_owner as owner_module
from app.db.authorization_ai_owner import AIAuthorizationOwner
from app.db.authorization_chat_owner import ChatOwner
from app.db.base import role_menus
from app.modules.ai.agents.hitl.manager import PendingPayload, hitl_manager
from app.modules.ai.api.resume import _pending_from_durable_action
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.system.models.menu import Menu
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


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("corrupt", [False, True])
async def test_orphan_only_settles_bound_readonly_logs(
    pending_case, monkeypatch, running, corrupt
):
    factory, plain, case = pending_case
    async with plain() as db:
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=9002,
                menu_name="Department read",
                menu_type="F",
                permission="system:dept:list",
                status="1",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=9002)
        )
        action = await db.scalar(select(AiPreparedAction))
        confirmation_id = action.confirmation_id
        pending = replace(
            _pending_from_durable_action(action), action_id=None, tool_name="dept.list"
        )
        await db.execute(delete(AiPreparedAction))
        await db.execute(
            update(AiOperationLog).values(
                tool_name="dept.list",
                readonly_snapshot=True,
                status="running" if running else "pending_confirmation",
                started_at=datetime.now(UTC).replace(tzinfo=None) if running else None,
                args_hash="f" * 64 if corrupt else action.args_hash,
            )
        )
        await db.commit()
    owner = AIAuthorizationOwner(factory)
    cutoff = await owner.cutoff()
    remove = AsyncMock()
    monkeypatch.setattr(hitl_manager, "delete_pending", remove)
    display_errors = []
    recover = ChatOwner.recover_orphan

    async def display(self, **request):
        try:
            return await recover(self, **request)
        except Exception as exc:
            display_errors.append(exc)
            raise

    monkeypatch.setattr(ChatOwner, "recover_orphan", display)
    locks = []

    def sql(_conn, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    engine = plain.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", sql)
    try:
        settled = await owner.orphan_cached(
            pending=pending,
            confirmation_id=confirmation_id,
            redis=object(),
            startup_before=cutoff,
        )
    finally:
        event.remove(engine, "before_cursor_execute", sql)
    assert settled is (not corrupt)
    if display_errors:
        raise display_errors[0]
    if not corrupt:
        await ChatOwner(factory).recover_orphan(
            tenant=case.tenant, confirmation_id=confirmation_id
        )
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    assert remove.await_count == (0 if corrupt else 1)
    async with plain() as db:
        log = await db.scalar(select(AiOperationLog))
        assert log.status == (
            ("running" if running else "pending_confirmation")
            if corrupt
            else ("failed" if running else "expired")
        )
        messages = list(
            await db.scalars(select(AiMessage).where(AiMessage.role == "assistant"))
        )
        assert len(messages) == (0 if corrupt else 1)
        if messages:
            card = messages[0].tool_calls[0]
            assert card["ok"] is False and card["result"] is None
            assert card["error_code"] == (
                "AI_HITL_EXECUTION_INTERRUPTED" if running else "AI_HITL_EXPIRED"
            )


@pytest.mark.parametrize("invalid", ["oversize", "encoding"])
async def test_platform_cache_reads_bound_bytes_before_parsing(invalid):
    pending = PendingPayload(
        101,
        0,
        1,
        "call",
        "trace",
        "dept.list",
        {"large": "x" * 70000},
        None,
        "2026-10-28T00:00:00Z",
    )
    body = pending.to_json_bytes() if invalid == "oversize" else b"\xff"
    redis = AsyncMock()
    redis.get.return_value = body
    redis.getrange.return_value = body[:65537]
    platform = PlatformContext(
        0,
        "ai-lifecycle",
        "service",
        frozenset({"platform:ai:lifecycle"}),
        "startup",
        "TEST",
        "startup-test",
    )
    assert (
        await hitl_manager.get_pending_for_platform(
            redis, "confirmation", tenant_id=0, platform=platform
        )
        is None
    )
    redis.get.assert_not_awaited()
    redis.getrange.assert_awaited_once_with(
        "ai:confirm:tenant:0:confirmation", 0, 65536
    )


@pytest.mark.parametrize("ack", ["known", "before", "after"])
async def test_orphan_unknown_ack_never_clears_cache_or_repairs_display(
    pending_case, monkeypatch, ack
):
    factory, plain, _ = pending_case
    async with plain() as db:
        action = await db.scalar(select(AiPreparedAction))
        confirmation = action.confirmation_id
        pending = replace(
            _pending_from_durable_action(action), action_id=None, tool_name="dept.list"
        )
        await db.execute(delete(AiPreparedAction))
        await db.execute(
            update(AiOperationLog).values(tool_name="dept.list", readonly_snapshot=True)
        )
        await db.commit()
    owner = AIAuthorizationOwner(factory)
    cutoff = await owner.cutoff()
    original = owner_module.commit_authorization_session

    async def commit(db):
        if ack != "before":
            await original(db)
        if ack != "known":
            raise RuntimeError("unknown acknowledgement")

    async def cache(*_args, **_kwargs):
        async with plain() as db:
            assert await db.scalar(select(AiOperationLog.status)) == "expired"

    monkeypatch.setattr(owner_module, "commit_authorization_session", commit)
    remove = AsyncMock(side_effect=cache)
    display = AsyncMock()
    monkeypatch.setattr(hitl_manager, "delete_pending", remove)
    monkeypatch.setattr(ChatOwner, "recover_orphan", display)
    settled = await owner.orphan_cached(
        pending=pending,
        confirmation_id=confirmation,
        redis=object(),
        startup_before=cutoff,
    )
    assert settled is (ack == "known")
    assert remove.await_count == display.await_count == (1 if ack == "known" else 0)
