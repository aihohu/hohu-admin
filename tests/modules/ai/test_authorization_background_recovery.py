"""Startup and periodic recovery retain pending proposals and fence only facts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, insert, select, update

from app.core import redis as redis_module
from app.core.exceptions import BusinessException
from app.core.tenant import PlatformContext
from app.db import authorization_ai_owner as owner_module
from app.db.authorization_ai_owner import (
    AIAuthorizationOwner,
    RecoveryItem,
    RecoveryPage,
)
from app.db.authorization_chat_owner import ChatOwner
from app.db.base import role_menus
from app.modules.ai import lifecycle
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.api.resume import _pending_from_durable_action
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.chat_run_service import chat_run_guard
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.auth.service import load_live_user_authority
from app.modules.system.ai_tools import (
    analytics,  # noqa: F401 - production registration
    dept,  # noqa: F401 - production registration
)
from app.modules.system.models.menu import Menu
from app.modules.system.models.user import User
from tests.modules.ai.test_authorization_lifecycle import (
    execution_case as execution_case,
)
from tests.modules.ai.test_authorization_lifecycle import (
    pending_case as pending_case,
)
from tests.modules.ai.test_authorization_lifecycle import (
    plan_sessions as plan_sessions,
)
from tests.modules.ai.test_authorization_lifecycle import (
    recovery_case as recovery_case,
)
from tests.modules.ai.test_authorization_lifecycle import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_candidate import epoch


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("valid_pending", "pending_confirmation"),
        ("expired_pending", "expired"),
        ("stale_pending", "expired"),
        ("readonly_approved", "failed"),
        ("readonly_expired", "failed"),
        ("readonly_live", "unknown"),
        ("db_live", "unknown"),
    ],
)
async def test_sweep_actual_states_use_g_first_and_never_execute(
    pending_case, scenario, expected, monkeypatch
):
    factory, plain, case = pending_case
    assert ToolRegistry.get().find("user.count") is not None
    handler = AsyncMock(
        side_effect=AssertionError("Recovery must not execute a handler")
    )
    monkeypatch.setattr(ToolRegistry.get().find("user.count"), "fn", handler)
    now = datetime.now(UTC)
    action_values = {"expires_at": now + timedelta(minutes=5)}
    log_values = {}
    if scenario == "expired_pending":
        action_values["expires_at"] = now - timedelta(seconds=1)
    if scenario.startswith("readonly_"):
        action_values.update(
            execute_tool_name="user.count",
            agent_code="user_mgmt",
            row_version=2,
            status="approved",
            approved_by=101,
            approved_at=now,
        )
        log_values.update(
            tool_name="user.count", agent_code="user_mgmt", readonly_snapshot=True
        )
    if scenario in {"readonly_expired", "readonly_live", "db_live"}:
        action_values.update(
            status="running",
            row_version=3,
            approved_by=101,
            approved_at=now,
            execution_owner="readonly-worker"
            if scenario.startswith("readonly")
            else case.claim.execution_owner,
            execution_lease_expires_at=now
            + timedelta(seconds=-1 if scenario == "readonly_expired" else 90),
        )
        log_values.update(
            status="running", started_at=now.replace(tzinfo=None), approved_by=101
        )
    async with plain() as db:
        await db.execute(update(AiPreparedAction).values(**action_values))
        if log_values:
            await db.execute(update(AiOperationLog).values(**log_values))
        if scenario == "stale_pending":
            await db.execute(update(AiMessage).values(is_active=False))
        await db.commit()
    locks = []

    def sql(_conn, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    engine = plain.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", sql)
    try:
        page = await AIAuthorizationOwner(factory).sweep_page(startup=True)
    finally:
        event.remove(engine, "before_cursor_execute", sql)
    assert len(page.items) == 1 and page.items[0].disposition == expected
    assert not hasattr(page.items[0], "action")
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    async with plain() as db:
        action = await db.scalar(select(AiPreparedAction))
        assert action.status == (
            action_values["status"] if expected == "unknown" else expected
        )
        assert await epoch(db) == 1
    handler.assert_not_awaited()


@pytest.mark.parametrize("ack", ["known", "before", "after"])
async def test_recovery_cache_effects_follow_only_known_completion(
    pending_case, monkeypatch, ack
):
    factory, plain, _ = pending_case
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
                guard_owner_token="original-guard",
            )
        )
        await db.commit()
    original = owner_module.commit_authorization_session

    async def uncertain(db):
        if ack != "before":
            await original(db)
        if ack != "known":
            raise RuntimeError("unknown commit acknowledgement")

    monkeypatch.setattr(owner_module, "commit_authorization_session", uncertain)
    active = set()

    def begin(connection):
        active.add(connection)

    def end(connection):
        active.discard(connection)

    async def cache(*_args, **_kwargs):
        assert not active
        async with plain() as db:
            assert await db.scalar(select(AiPreparedAction.status)) == "expired"

    release, remove = AsyncMock(side_effect=cache), AsyncMock(side_effect=cache)
    display = AsyncMock(side_effect=RuntimeError("display unavailable"))
    monkeypatch.setattr(ChatOwner, "recover", display)
    monkeypatch.setattr(chat_run_guard, "release", release)
    monkeypatch.setattr(hitl_manager, "delete_pending", remove)
    engine = plain.kw["bind"].sync_engine
    listeners = [("begin", begin), ("rollback", end), ("commit", end)]
    for name, listener in listeners:
        event.listen(engine, name, listener)
    try:
        page = await AIAuthorizationOwner(factory).sweep_page(
            startup=True, redis=object(), repair_display=True
        )
    finally:
        for name, listener in listeners:
            event.remove(engine, name, listener)
    assert page.items[0].disposition == ("expired" if ack == "known" else "unknown")
    assert release.await_count == remove.await_count == (1 if ack == "known" else 0)
    assert display.await_count == (1 if ack == "known" else 0)


@pytest.mark.parametrize("revoked", [False, True])
@pytest.mark.parametrize("readonly", [False, True])
async def test_recovered_display_rechecks_current_authority(
    pending_case, revoked, readonly
):
    factory, plain, case = pending_case
    async with plain() as db:
        tool = "dept.list" if readonly else "dept.create"
        assert ToolRegistry.get().find(tool) is not None
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
        actor = await load_live_user_authority(db, tenant=case.tenant)
        lineage = result_projection_service.freeze_lineage(
            tenant=case.tenant,
            agent_code="dept_mgmt",
            tool_codes=[tool],
            subject_refs=[],
            data_scope_hash=await result_projection_service.compute_data_scope_hash(
                db, actor
            ),
        )
        assert await result_projection_service._authorize_agent_and_tools(
            db, actor, lineage
        )
        assert await result_projection_service.authorize_result_projection(
            db, actor, owner_user_id=101, lineage=lineage
        )
        await db.execute(
            update(AiPreparedAction).values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
                execute_tool_name=tool,
                tool_codes=[tool],
                subject_refs=[],
                subject_refs_hash=lineage.subject_refs_hash,
                resolver_version=lineage.resolver_version,
                data_scope_hash=lineage.data_scope_hash,
                projection_dependency_message_ids=[],
            )
        )
        await db.execute(
            update(AiOperationLog).values(tool_name=tool, readonly_snapshot=readonly)
        )
        await db.commit()
    page = await AIAuthorizationOwner(factory).sweep_page(startup=True)
    assert page.items[0].disposition == "expired"
    if revoked:
        async with plain() as db:
            await db.execute(
                update(User)
                .where(User.user_id == 101)
                .values(status="2", auth_version=9)
            )
            await db.commit()
        with pytest.raises(BusinessException):
            await ChatOwner(factory).recover(
                tenant=case.tenant, confirmation_id=case.action.confirmation_id
            )
    else:
        await ChatOwner(factory).recover(
            tenant=case.tenant, confirmation_id=case.action.confirmation_id
        )
    async with plain() as db:
        messages = list(
            await db.scalars(select(AiMessage).where(AiMessage.role == "assistant"))
        )
        assert len(messages) == (0 if revoked else 1)
        if messages:
            card = messages[0].tool_calls[0]
            assert card["ok"] is False and card["error_code"] == "AI_HITL_EXPIRED"
        assert await db.scalar(select(AiPreparedAction.status)) == "expired"


async def test_api_startup_entry_uses_planned_recovery(pending_case, monkeypatch):
    factory, plain, _ = pending_case
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db.commit()
    runtime = SimpleNamespace(recovery=AIAuthorizationOwner(factory))
    monkeypatch.setattr(lifecycle, "get_authorization_runtime", lambda: runtime)
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
        await lifecycle.cleanup_prepared_actions_on_startup(
            AsyncMock(), platform=platform
        )
        == 1
    )
    async with plain() as db:
        assert await db.scalar(select(AiPreparedAction.status)) == "expired"


async def test_startup_cutoff_preserves_newly_approved_readonly(pending_case):
    factory, plain, _ = pending_case
    owner = AIAuthorizationOwner(factory)
    cutoff = await owner.cutoff()
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                execute_tool_name="dept.list",
                status="approved",
                row_version=2,
                approved_by=101,
                approved_at=cutoff + timedelta(seconds=1),
            )
        )
        await db.execute(
            update(AiOperationLog).values(tool_name="dept.list", readonly_snapshot=True)
        )
        await db.commit()
    page = await owner.sweep_page(startup=True, startup_before=cutoff)
    assert len(page.items) == 1 and page.items[0].disposition == "unknown"
    async with plain() as db:
        assert await db.scalar(select(AiPreparedAction.status)) == "approved"


async def test_periodic_scan_finishes_startup_pages_before_normal_scan(monkeypatch):
    cutoff = datetime.now(UTC)
    owner = SimpleNamespace(
        cutoff=AsyncMock(return_value=cutoff),
        sweep_page=AsyncMock(
            side_effect=[
                RecoveryPage((RecoveryItem(0, 1, "unknown"),), (0, 1)),
                RecoveryPage((RecoveryItem(0, 2, "expired", True),), None),
                RecoveryPage((), None),
            ]
        ),
    )
    monkeypatch.setattr(
        lifecycle, "get_authorization_runtime", lambda: SimpleNamespace(recovery=owner)
    )
    monkeypatch.setattr(lifecycle, "_recovery_after", None)
    monkeypatch.setattr(lifecycle, "_recovery_startup_before", None)
    platform = PlatformContext(
        0,
        "ai-lifecycle",
        "service",
        frozenset({"platform:ai:lifecycle"}),
        "startup",
        "TEST",
        "startup-test",
    )
    redis = object()
    assert (
        await lifecycle.cleanup_prepared_actions_on_startup(redis, platform=platform)
        == 0
    )
    assert (
        await lifecycle.cleanup_prepared_actions_on_startup(
            redis, platform=platform, expired_only=True
        )
        == 1
    )
    assert (
        await lifecycle.cleanup_prepared_actions_on_startup(
            redis, platform=platform, expired_only=True
        )
        == 0
    )
    calls = [c.kwargs for c in owner.sweep_page.await_args_list]
    assert [c["after"] for c in calls] == [None, (0, 1), None]
    assert [c["startup_before"] for c in calls] == [cutoff, cutoff, None]
    assert [c["startup"] for c in calls] == [True, True, False]
    owner.cutoff.assert_awaited_once()


@pytest.mark.parametrize("corrupt", [False, True])
async def test_cached_terminal_is_reverified_under_fresh_plan(
    pending_case, monkeypatch, corrupt
):
    factory, plain, case = pending_case
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db.commit()
    owner = AIAuthorizationOwner(factory)
    assert (await owner.sweep_page()).items[0].disposition == "expired"
    if corrupt:
        async with plain() as db:
            await db.execute(update(AiOperationLog).values(args_hash="f" * 64))
            await db.commit()
    remove = AsyncMock()
    monkeypatch.setattr(hitl_manager, "delete_pending", remove)
    item = await owner.observe_cached(
        tenant_id=0,
        user_id=101,
        confirmation_id=case.action.confirmation_id,
        redis=object(),
    )
    assert item.disposition == ("unknown" if corrupt else "expired")
    assert remove.await_count == (0 if corrupt else 1)


async def test_memory_cache_entry_uses_fixed_owner(pending_case, monkeypatch):
    factory, plain, _ = pending_case
    owner = AIAuthorizationOwner(factory)
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db.commit()
    assert (await owner.sweep_page()).items[0].disposition == "expired"
    async with plain() as db:
        action = await db.scalar(select(AiPreparedAction))
        pending = _pending_from_durable_action(action)
        confirmation_id = action.confirmation_id

    async def keys(**_kwargs):
        yield f"ai:confirm:tenant:0:{confirmation_id}"

    monkeypatch.setattr(redis_module.redis_client, "scan_iter", keys)
    monkeypatch.setattr(
        lifecycle, "get_authorization_runtime", lambda: SimpleNamespace(recovery=owner)
    )
    monkeypatch.setattr(
        lifecycle, "cleanup_prepared_actions_on_startup", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        lifecycle,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(
            AssertionError("No ordinary cleanup transaction")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        hitl_manager, "get_pending_for_platform", AsyncMock(return_value=pending)
    )
    remove = AsyncMock()
    monkeypatch.setattr(hitl_manager, "delete_pending", remove)
    platform = PlatformContext(
        0,
        "ai-lifecycle",
        "service",
        frozenset({"platform:ai:lifecycle"}),
        "startup",
        "TEST",
        "startup-test",
    )
    assert await lifecycle.cleanup_orphaned_pending_on_startup(platform=platform) == 1
    remove.assert_awaited_once()


async def test_verified_readonly_success_clears_cache_without_inventing_display(
    pending_case, monkeypatch
):
    factory, plain, case = pending_case
    now = datetime.now(UTC)
    async with plain() as db:
        await db.execute(
            update(AiPreparedAction).values(
                execute_tool_name="dept.list",
                status="succeeded",
                row_version=4,
                approved_by=101,
                approved_at=now,
                finished_at=now,
                duration_ms=0,
            )
        )
        await db.execute(
            update(AiOperationLog).values(
                tool_name="dept.list",
                readonly_snapshot=True,
                status="success",
                approved_by=101,
                started_at=now.replace(tzinfo=None),
                finished_at=now.replace(tzinfo=None),
            )
        )
        await db.commit()
    remove = AsyncMock()
    monkeypatch.setattr(hitl_manager, "delete_pending", remove)
    item = await AIAuthorizationOwner(factory).observe_cached(
        tenant_id=0,
        user_id=101,
        confirmation_id=case.action.confirmation_id,
        redis=object(),
    )
    assert item.disposition == "succeeded"
    remove.assert_awaited_once()
    async with plain() as db:
        assert (
            await db.scalar(
                select(AiMessage.message_id).where(AiMessage.role == "assistant")
            )
            is None
        )
