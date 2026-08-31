"""execute_tool 的 HITL、流式协议和审计集成测试。

覆盖：
  - tool not found / perm denied 短路返回 ToolResult.failure
  - autonomous 流：emit tool_call_started + tool_call_result + 写 ai_operation_log
  - HITL 流（mock hitl_manager.hang）：emit confirmation_required + 接受 wake
"""

# ruff: noqa: ARG001, PLC0415

import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select, text

from app.core import redis as redis_module
from app.core.config import settings
from app.core.exceptions import AuthorizationException
from app.modules.ai.agents.gateway.executor import execute_tool
from app.modules.ai.agents.gateway.result import (
    PreparedActionProposal,
    ResultProjection,
    ToolResult,
)
from app.modules.ai.agents.hitl.constants import ConfirmAction, DryRunResult
from app.modules.ai.agents.hitl.events import (
    ConfirmationRequiredEvent,
    ToolCallResultEvent,
    ToolCallStartedEvent,
)
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.agents.tools.registry import ToolRegistry
from app.modules.ai.core.context import ChatDeps, DataScopeContext


@pytest.fixture(autouse=True)
async def clean_env(monkeypatch):
    """每个测试前：重建 redis_client + 清 Redis + reset hitl_manager + 清本轮测试日志。

    ai_operation_log 不能用 TRUNCATE（会清掉生产 AI 审计日志）。所有测试代码
    通过 _build_deps 写入的行 trace_id 都以 'tr_test_' 前缀开头（如 tr_test_001 /
    tr_test_agent_pass / tr_test_agent_full），用 LIKE 精准清理，生产数据保持不动。
    """
    original_pool = redis_module.redis_pool
    original_client = redis_module.redis_client

    redis_module.redis_pool = aioredis.ConnectionPool.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20,
    )
    redis_module.redis_client = aioredis.Redis(connection_pool=redis_module.redis_pool)

    from app.modules.ai.agents.gateway import executor as exec_mod

    exec_mod.redis_client = redis_module.redis_client

    for pattern in [
        "ai:confirm:*",
        "ai:write:*",
        "ai:quota:*",
        "ai:failures:*",
        "ai:query_cache:*",
    ]:
        keys = await redis_module.redis_client.keys(pattern)
        if keys:
            await redis_module.redis_client.delete(*keys)

    hitl_manager._reset_for_test()

    from app.modules.ai.service.prepared_action_service import prepared_action_service

    monkeypatch.setattr(
        prepared_action_service,
        "lock_source_binding",
        AsyncMock(return_value=True),
    )

    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(
                text("DELETE FROM ai_prepared_action WHERE trace_id LIKE 'tr_test_%'")
            )
            await db.execute(
                text("DELETE FROM ai_operation_log WHERE trace_id LIKE 'tr_test_%'")
            )

    yield

    for pattern in [
        "ai:confirm:*",
        "ai:write:*",
        "ai:quota:*",
        "ai:failures:*",
        "ai:query_cache:*",
    ]:
        keys = await redis_module.redis_client.keys(pattern)
        if keys:
            await redis_module.redis_client.delete(*keys)
    hitl_manager._reset_for_test()

    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(
                text("DELETE FROM ai_prepared_action WHERE trace_id LIKE 'tr_test_%'")
            )
            await db.execute(
                text("DELETE FROM ai_operation_log WHERE trace_id LIKE 'tr_test_%'")
            )

    # 释放连接池避免跨测试 event loop 干扰
    from app.db.session import engine

    try:
        await engine.dispose()
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise

    redis_module.redis_pool = original_pool
    redis_module.redis_client = original_client
    exec_mod.redis_client = original_client


# ============ 临时测试 tool（注册到 Registry） ============

_TEST_TOOL_LOW = "testint.echo_low"  # autonomous
_TEST_TOOL_HIGH = "testint.echo_high"  # HITL（risk=high + count=None）
_TEST_TOOL_PERMED = "testint.perm_required"  # 用于 perm denied 测试
_TEST_TOOL_READONLY = "testint.readonly_list"  # 写 query_cache
_TEST_TOOL_PREPARE = "testint.import_preview"  # prepared preview
_TEST_TOOL_PREPARED_EXECUTE = "testint.import_execute"  # bound HITL execute
_TEST_TOOL_FREEZE_DIRECT = "testint.freeze_direct"  # direct HITL exact binding
_TEST_TOOL_CANONICALIZE_DIRECT = "testint.canonicalize_direct"
_TEST_TOOL_DRY_RUN_DENIED = "testint.dry_run_denied"

_TOOLS_REGISTERED = False


def _register_test_tools() -> None:
    """注册临时测试 tool（首次调用注册，之后跳过）"""
    global _TOOLS_REGISTERED
    if _TOOLS_REGISTERED:
        return
    _TOOLS_REGISTERED = True

    @ai_tool(
        AiToolMeta(
            name=_TEST_TOOL_LOW,
            agent="shared",
            summary="test low risk",
            required_perms=(),
            risk="low",
        )
    )
    async def _echo_low(ctx, **kwargs: Any) -> dict[str, Any]:
        return {"echo": kwargs}

    @ai_tool(
        AiToolMeta(
            name=_TEST_TOOL_HIGH,
            agent="shared",
            summary="test high risk",
            required_perms=(),
            risk="high",
            projection_kind="none",
        )
    )
    async def _echo_high(ctx, **kwargs: Any) -> dict[str, Any]:
        return {"echo": kwargs}

    @ai_tool(
        AiToolMeta(
            name=_TEST_TOOL_PERMED,
            agent="shared",
            summary="test perm required",
            required_perms=("testint:fake_perm",),
            risk="low",
        )
    )
    async def _echo_permed(ctx, **kwargs: Any) -> dict[str, Any]:
        return {"echo": kwargs}

    @ai_tool(
        AiToolMeta(
            name=_TEST_TOOL_READONLY,
            agent="shared",
            summary="test readonly + query_cache",
            required_perms=(),
            risk="low",
            readonly=True,
            allowed_filters=("status", "user_gender"),
            query_cache_module="system/user",
        )
    )
    async def _readonly_list(ctx, **kwargs: Any) -> ToolResult:
        return ToolResult.success(
            {"count": 0},
            projection=ResultProjection(),
        )

    @ai_tool(
        AiToolMeta(
            name=_TEST_TOOL_PREPARE,
            agent="shared",
            summary="prepare a test import",
            required_perms=(),
            risk="low",
            interaction_flow="prepared",
            prepared_execute_tool=_TEST_TOOL_PREPARED_EXECUTE,
        )
    )
    async def _prepare_import(ctx, resource_id: str) -> ToolResult:
        return ToolResult.success(
            data={"total": 2, "summary": {"new": 2, "exists": 0}},
            prepared_action=PreparedActionProposal(
                frozen_args={
                    "preview_token": "server-only-token",
                    "reason": "test import",
                },
                snapshot={"batch_id": "batch-1", "new": 2, "exists": 0},
                subject_ref={"type": "test_import", "id": "batch-1"},
                presentation={
                    "title": "Import 2 users",
                    "fields": [
                        {"label": "new", "value": 2},
                        {"label": "exists", "value": 0},
                    ],
                    "warnings": [],
                },
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            ),
        )

    @ai_tool(
        AiToolMeta(
            name=_TEST_TOOL_PREPARED_EXECUTE,
            agent="shared",
            summary="execute a prepared test import",
            required_perms=(),
            risk="high",
            projection_kind="none",
            hitl_always=True,
            llm_visible=False,
        )
    )
    async def _execute_import(ctx, **kwargs: Any) -> ToolResult:
        return ToolResult.success(data={"successCount": 2})

    @ai_tool(
        AiToolMeta(
            name=_TEST_TOOL_FREEZE_DIRECT,
            agent="shared",
            summary="freeze a direct destructive target",
            required_perms=(),
            risk="high",
            projection_kind="none",
            hitl_always=True,
            dry_run_supported=True,
        )
    )
    async def _freeze_direct(ctx, **kwargs: Any) -> ToolResult:
        return ToolResult.success(data={"deleted": 1})

    async def _dry_run_freeze_direct(ctx, **kwargs: Any) -> DryRunResult:
        return DryRunResult(
            ok=True,
            count=1,
            reason="delete one exact target",
            execution_args={"target_ids": [7001]},
            business_snapshot={"targets": [{"id": "7001", "name": "approved"}]},
        )

    ToolRegistry.get().set_dry_run_fn(_TEST_TOOL_FREEZE_DIRECT, _dry_run_freeze_direct)

    @ai_tool(
        AiToolMeta(
            name=_TEST_TOOL_CANONICALIZE_DIRECT,
            agent="shared",
            summary="freeze a semantic value to its canonical code",
            required_perms=(),
            risk="high",
            projection_kind="none",
            hitl_always=True,
            dry_run_supported=True,
            args_summary_fields=("scope",),
        )
    )
    async def _canonicalize_direct(ctx, *, scope: str) -> ToolResult:
        return ToolResult.success(data={"scope": scope})

    async def _dry_run_canonicalize_direct(ctx, *, scope: str) -> DryRunResult:
        assert scope == "SELF"
        return DryRunResult(
            ok=True,
            count=1,
            reason="store canonical scope",
            confirmation_fields=[
                {"label": "scope", "value": "5", "display_value": "SELF (5)"}
            ],
            execution_args={"scope": "5"},
            business_snapshot={"scope": "5"},
        )

    ToolRegistry.get().set_dry_run_fn(
        _TEST_TOOL_CANONICALIZE_DIRECT,
        _dry_run_canonicalize_direct,
    )

    @ai_tool(
        AiToolMeta(
            name=_TEST_TOOL_DRY_RUN_DENIED,
            agent="shared",
            summary="reject a direct tool during dry run",
            required_perms=(),
            risk="high",
            projection_kind="none",
            hitl_always=True,
            dry_run_supported=True,
        )
    )
    async def _dry_run_denied_tool(ctx, **kwargs: Any) -> ToolResult:
        return ToolResult.success(data={"updated": 1})

    async def _dry_run_denied(ctx, **kwargs: Any) -> DryRunResult:
        raise AuthorizationException(
            "target is outside the current scope",
            error_code="AI_DATA_SCOPE_VIOLATION",
        )

    ToolRegistry.get().set_dry_run_fn(_TEST_TOOL_DRY_RUN_DENIED, _dry_run_denied)


def _build_deps(
    *,
    perms: set[str] | None = None,
    signal_event: Callable[[Any], Awaitable[None]] | None = None,
    agent_daily_quota: int | None = None,
    agent_code: str = "shared",
) -> ChatDeps:
    """构造测试 ChatDeps（mock user + 空 data_scope）"""
    user = MagicMock()
    user.user_id = 9001

    data_scope = DataScopeContext(
        accessible_dept_ids=None, accessible_user_scope=None, filters=[]
    )
    agent = MagicMock()
    agent.code = agent_code
    agent.daily_quota_per_user = agent_daily_quota  # Agent 级日配额。

    return ChatDeps(
        user=user,
        perms=perms if perms is not None else {"*"},
        db=MagicMock(),
        data_scope=data_scope,
        agent=agent,
        trace_id="tr_test_001",
        tenant_id=77,
        conversation_id=100,
        source_user_message_id=101,
        signal_event=signal_event,
        resolved_model_id=7001,
        resolved_provider_id=8001,
    )


# ============ _infer_affected_rows helper ============


class TestInferAffectedRows:
    """验证结果卡片 affected_rows 的来源规则。"""

    def test_dry_run_count_takes_priority(self) -> None:
        from app.modules.ai.agents.gateway.executor import _infer_affected_rows

        # dry_run_count=3，result_data 也有 count=99 → 取 dry_run_count
        assert _infer_affected_rows(dry_run_count=3, result_data={"count": 99}) == 3

    def test_dict_with_count(self) -> None:
        from app.modules.ai.agents.gateway.executor import _infer_affected_rows

        assert _infer_affected_rows(dry_run_count=None, result_data={"count": 23}) == 23

    def test_dict_with_affected_count(self) -> None:
        from app.modules.ai.agents.gateway.executor import _infer_affected_rows

        assert (
            _infer_affected_rows(dry_run_count=None, result_data={"affected_count": 5})
            == 5
        )

    def test_dict_with_groups_count(self) -> None:
        """stats tool 返回 {groups_count: 2}"""
        from app.modules.ai.agents.gateway.executor import _infer_affected_rows

        assert (
            _infer_affected_rows(dry_run_count=None, result_data={"groups_count": 2})
            == 2
        )

    def test_list_length(self) -> None:
        """result 是 list → 长度"""
        from app.modules.ai.agents.gateway.executor import _infer_affected_rows

        assert _infer_affected_rows(dry_run_count=None, result_data=[1, 2, 3, 4]) == 4

    def test_dict_without_known_key_returns_none(self) -> None:
        from app.modules.ai.agents.gateway.executor import _infer_affected_rows

        assert (
            _infer_affected_rows(
                dry_run_count=None, result_data={"echo": {"msg": "hi"}}
            )
            is None
        )

    def test_scalar_returns_none(self) -> None:
        from app.modules.ai.agents.gateway.executor import _infer_affected_rows

        assert _infer_affected_rows(dry_run_count=None, result_data=42) is None

    def test_none_result_returns_none(self) -> None:
        """失败路径 result=None"""
        from app.modules.ai.agents.gateway.executor import _infer_affected_rows

        assert _infer_affected_rows(dry_run_count=None, result_data=None) is None

    def test_bool_in_dict_ignored(self) -> None:
        """dict 含布尔值的 count 不当作行数（避免 True/False 误判为 1/0）"""
        from app.modules.ai.agents.gateway.executor import _infer_affected_rows

        assert (
            _infer_affected_rows(
                dry_run_count=None, result_data={"count": True, "name": "x"}
            )
            is None
        )


# ============ tool not found / perm denied ============


# ============ build_args_summary 白名单字段 ============


class TestBuildArgsSummary:
    """args_summary 只包含元信息和可选白名单字段。"""

    def test_mvp_default_no_fields(self) -> None:
        """默认不传 args / summary_fields → 仅元信息（MVP 行为）"""
        from app.modules.ai.agents.gateway.executor import build_args_summary

        result = build_args_summary(
            "user.update_dept",
            risk_level="high",
            execution_mode="hitl",
            dry_run_count=1,
        )
        assert result == "tool=user.update_dept, risk=high, mode=hitl, dry_run_count=1"

    def test_summary_fields_empty_tuple_no_extract(self) -> None:
        """传 args 但 summary_fields=() → 不提取（与 MVP 等价）"""
        from app.modules.ai.agents.gateway.executor import build_args_summary

        result = build_args_summary(
            "user.update_dept",
            risk_level="high",
            execution_mode="hitl",
            dry_run_count=1,
            args={"user_id": 42, "new_dept_id": 8, "password": "secret"},
            summary_fields=(),
        )
        # 不提取任何字段，即使 args 中有
        assert "user_id" not in result
        assert "password" not in result
        assert result == "tool=user.update_dept, risk=high, mode=hitl, dry_run_count=1"

    def test_summary_fields_extract_only_declared(self) -> None:
        """声明 ('user_id', 'new_dept_id') → 仅提取这两个字段"""
        from app.modules.ai.agents.gateway.executor import build_args_summary

        result = build_args_summary(
            "user.update_dept",
            risk_level="high",
            execution_mode="hitl",
            dry_run_count=1,
            args={"user_id": 42, "new_dept_id": 8, "reason": "test"},
            summary_fields=("user_id", "new_dept_id"),
        )
        assert "user_id=42" in result
        assert "new_dept_id=8" in result
        # 未声明的 reason 不应出现
        assert "reason" not in result

    def test_summary_fields_missing_in_args_skipped(self) -> None:
        """声明字段在 args 中不存在 → 跳过（不抛 KeyError）"""
        from app.modules.ai.agents.gateway.executor import build_args_summary

        result = build_args_summary(
            "user.update_dept",
            risk_level="high",
            execution_mode="hitl",
            dry_run_count=None,
            args={"user_id": 42},
            summary_fields=("user_id", "new_dept_id"),  # new_dept_id 不在 args
        )
        assert "user_id=42" in result
        assert "new_dept_id" not in result

    def test_repr_wraps_string_values(self) -> None:
        """str 值用 repr() 包裹（区分 str 与 int）"""
        from app.modules.ai.agents.gateway.executor import build_args_summary

        result = build_args_summary(
            "role.update",
            risk_level="high",
            execution_mode="autonomous",
            dry_run_count=1,
            args={"role_code": "R_ADMIN", "user_count": 5},
            summary_fields=("role_code", "user_count"),
        )
        # str 用引号，int 不用
        assert "role_code='R_ADMIN'" in result
        assert "user_count=5" in result

    def test_dry_run_count_none_omitted(self) -> None:
        """dry_run_count=None 时省略 dry_run_count 段"""
        from app.modules.ai.agents.gateway.executor import build_args_summary

        result = build_args_summary(
            "user.lookup",
            risk_level="low",
            execution_mode="autonomous",
            dry_run_count=None,
        )
        assert "dry_run_count" not in result
        assert result == "tool=user.lookup, risk=low, mode=autonomous"

    def test_args_none_with_summary_fields_no_extract(self) -> None:
        """args=None + summary_fields 非空 → 不提取（无源数据）"""
        from app.modules.ai.agents.gateway.executor import build_args_summary

        result = build_args_summary(
            "user.update_dept",
            risk_level="high",
            execution_mode="hitl",
            dry_run_count=1,
            args=None,
            summary_fields=("user_id",),
        )
        assert "user_id" not in result
        assert result == "tool=user.update_dept, risk=high, mode=hitl, dry_run_count=1"


# ============ tool not found / perm denied ============


class TestShortCircuit:
    def test_public_executor_does_not_accept_prepared_capability(self) -> None:
        assert (
            "_prepared_action_context" not in inspect.signature(execute_tool).parameters
        )

    async def test_tool_not_found(self) -> None:
        deps = _build_deps()
        result = await execute_tool("nonexistent.tool", {}, deps)
        assert not result.ok
        assert result.error_code == "AI_TOOL_NOT_FOUND"

    async def test_perm_denied(self) -> None:
        """required_perms 不在 user perms 中 → perm denied"""
        _register_test_tools()
        deps = _build_deps(perms=set())  # 空 perms
        result = await execute_tool(_TEST_TOOL_PERMED, {"x": 1}, deps)
        assert not result.ok
        assert result.error_code == "AI_TOOL_PERM_DENIED"

    async def test_gateway_only_execute_rejects_direct_model_path(self) -> None:
        _register_test_tools()

        result = await execute_tool(
            _TEST_TOOL_PREPARED_EXECUTE,
            {"preview_token": "guessed"},
            _build_deps(),
        )

        assert not result.ok
        assert result.error_code == "AI_PREPARED_ACTION_REQUIRED"


# ============ autonomous 流 ============


class TestAutonomousFlow:
    async def test_emits_started_and_result(self) -> None:
        """autonomous 流发送 tool_call_started 和 tool_call_result。

        started 透传 risk；result 包含 duration_ms 和 affected_rows。
        """
        _register_test_tools()

        events: list[Any] = []

        async def collect(ev: Any) -> None:
            events.append(ev)

        deps = _build_deps(signal_event=collect)
        result = await execute_tool(_TEST_TOOL_LOW, {"msg": "hi"}, deps)

        assert result.ok
        assert result.data == {"echo": {"msg": "hi"}}

        assert len(events) == 2
        assert isinstance(events[0], ToolCallStartedEvent)
        assert events[0].tool == _TEST_TOOL_LOW
        assert events[0].args == {"msg": "hi"}
        assert events[0].risk == "low"
        assert isinstance(events[1], ToolCallResultEvent)
        assert events[1].ok is True
        # duration_ms 是实测墙钟耗时，必定是 int 且 ≥ 0
        assert isinstance(events[1].duration_ms, int)
        assert events[1].duration_ms >= 0
        # test tool 返回 {"echo": {...}}，无 affected_rows 信号 → None
        assert events[1].affected_rows is None

    async def test_writes_ai_operation_log(self) -> None:
        """每次 autonomous 工具调用写入一条成功操作日志。"""
        _register_test_tools()

        deps = _build_deps()
        result = await execute_tool(_TEST_TOOL_LOW, {}, deps)
        assert result.ok

        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            res = await db.execute(
                text(
                    "SELECT tool_name, status, execution_mode FROM ai_operation_log "
                    "WHERE trace_id = 'tr_test_001' "
                    "ORDER BY log_id DESC LIMIT 1"
                )
            )
            row = res.first()
            assert row is not None
            assert row.tool_name == _TEST_TOOL_LOW
            assert row.status == "success"
            assert row.execution_mode == "autonomous"


# ============ prepared preview-only flow ============


class TestPreparedPreviewOnlyFlow:
    async def test_preview_only_discards_capability_and_creates_no_action(
        self,
    ) -> None:
        """Preview-only 是终态投影，不能升级为执行。"""
        _register_test_tools()
        events: list[Any] = []

        async def collect(event: Any) -> None:
            events.append(event)

        deps = _build_deps(signal_event=collect)
        result = await execute_tool(
            _TEST_TOOL_PREPARE,
            {
                "resource_id": "file-preview-only",
                "requested_outcome": "preview_only",
            },
            deps,
        )

        assert result.ok
        assert result.data == {"total": 2, "summary": {"new": 2, "exists": 0}}
        assert result.prepared_action is None
        assert [type(event) for event in events] == [
            ToolCallStartedEvent,
            ToolCallResultEvent,
        ]
        assert not await redis_module.redis_client.keys("ai:confirm:*")

        from app.db.session import AsyncSessionLocal
        from app.modules.ai.models.prepared_action import AiPreparedAction

        async with AsyncSessionLocal() as db:
            action = (
                await db.execute(
                    select(AiPreparedAction).where(
                        AiPreparedAction.trace_id == deps.trace_id
                    )
                )
            ).scalar_one_or_none()

        assert action is None

    async def test_later_execute_intent_must_run_a_new_preview(
        self, monkeypatch
    ) -> None:
        """历史预检结果不能被提升为执行动作。"""
        _register_test_tools()
        events: list[Any] = []

        async def collect(event: Any) -> None:
            events.append(event)

        deps = _build_deps(signal_event=collect)
        preview = await execute_tool(
            _TEST_TOOL_PREPARE,
            {
                "resource_id": "file-preview-only",
                "requested_outcome": "preview_only",
            },
            deps,
        )
        assert preview.ok
        assert preview.prepared_action is None

        direct_execute = await execute_tool(
            _TEST_TOOL_PREPARED_EXECUTE,
            {"preview_token": "server-only-token"},
            deps,
        )
        assert not direct_execute.ok
        assert direct_execute.error_code == "AI_PREPARED_ACTION_REQUIRED"

        async def fake_hang(confirmation_id, *, timeout_sec=None):
            return ConfirmAction.REJECTED

        async def fake_terminal_result(confirmation_id):
            return ToolResult.failure("USER_REJECTED", "cancelled"), 0

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)
        monkeypatch.setattr(
            "app.modules.ai.agents.gateway.executor._load_prepared_terminal_result",
            fake_terminal_result,
        )
        execute_intent = await execute_tool(
            _TEST_TOOL_PREPARE,
            {
                "resource_id": "file-preview-only",
                "requested_outcome": "execute_if_approved",
            },
            deps,
        )

        assert not execute_intent.ok
        assert execute_intent.error_code == "USER_REJECTED"
        prepare_events = [
            event
            for event in events
            if isinstance(event, ToolCallStartedEvent)
            and event.tool == _TEST_TOOL_PREPARE
        ]
        assert len(prepare_events) == 2
        assert prepare_events[0].tool_call_id != prepare_events[1].tool_call_id
        confirmation = next(
            event for event in events if isinstance(event, ConfirmationRequiredEvent)
        )

        from app.db.session import AsyncSessionLocal
        from app.modules.ai.models.prepared_action import AiPreparedAction

        async with AsyncSessionLocal() as db:
            action = (
                await db.execute(
                    select(AiPreparedAction).where(
                        AiPreparedAction.confirmation_id == confirmation.confirmation_id
                    )
                )
            ).scalar_one()

        assert action.prepare_tool_call_id == prepare_events[1].tool_call_id


# ============ HITL 流（mock hitl_manager.hang 立即返回） ============


class TestHitlFlow:
    async def test_action_persistence_failure_rolls_back_pending_handoff(
        self, monkeypatch
    ) -> None:
        """没有持久化动作时，不创建 Redis pending 或 handed-off guard。"""
        from app.modules.ai.service.chat_run_service import chat_run_guard
        from app.modules.ai.service.prepared_action_service import (
            prepared_action_service,
        )

        _register_test_tools()
        deps = _build_deps()
        deps.guard_owner_token = "guard-owner-test"

        handoff = AsyncMock(return_value=True)
        release = AsyncMock(return_value=True)
        delete_pending = AsyncMock()
        rollback_quota = AsyncMock()
        persist = AsyncMock(side_effect=RuntimeError("action persistence failed"))
        monkeypatch.setattr(chat_run_guard, "handoff_pending", handoff)
        monkeypatch.setattr(chat_run_guard, "release", release)
        monkeypatch.setattr(hitl_manager, "delete_pending", delete_pending)
        monkeypatch.setattr(prepared_action_service, "create_pending", persist)
        monkeypatch.setattr(
            "app.modules.ai.agents.gateway.executor.decr_quota", rollback_quota
        )

        with pytest.raises(RuntimeError, match="action persistence failed"):
            await execute_tool(_TEST_TOOL_HIGH, {"x": 1}, deps)

        handoff.assert_awaited_once()
        delete_pending.assert_awaited_once()
        release.assert_awaited_once_with(
            redis_module.redis_client,
            conversation_id=100,
            owner_token="guard-owner-test",
        )
        assert deps.guard_handoff is False
        rollback_quota.assert_awaited_once()

        from app.db.session import AsyncSessionLocal
        from app.modules.ai.models.operation_log import AiOperationLog

        async with AsyncSessionLocal() as db:
            log = (
                await db.execute(
                    select(AiOperationLog).where(
                        AiOperationLog.trace_id == deps.trace_id,
                        AiOperationLog.tool_name == _TEST_TOOL_HIGH,
                    )
                )
            ).scalar_one()
        assert log.status == "expired"

    def test_direct_action_revalidates_current_gateway_binding(self) -> None:
        from types import SimpleNamespace

        from app.modules.ai.agents.gateway.executor import (
            validate_prepared_execution,
        )
        from app.modules.ai.agents.gateway.failures import compute_args_hash

        _register_test_tools()
        frozen_args = {"x": 1}
        action = SimpleNamespace(
            interaction_flow="direct",
            execute_tool_name=_TEST_TOOL_HIGH,
            frozen_args=frozen_args,
            args_hash=compute_args_hash(frozen_args),
            agent_code="shared",
        )
        deps = _build_deps()
        deps.agent.enabled = True

        registered = validate_prepared_execution(action, deps)

        assert registered.meta.name == _TEST_TOOL_HIGH

    async def test_prepared_preview_auto_enters_confirmation(self, monkeypatch) -> None:
        """一次明确的执行意图即可进入 HITL。"""
        _register_test_tools()

        async def fake_hang(confirmation_id, *, timeout_sec=None):
            return ConfirmAction.APPROVED

        async def fake_terminal_result(confirmation_id):
            return ToolResult.success({"successCount": 2}), 1

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)
        monkeypatch.setattr(
            "app.modules.ai.agents.gateway.executor._load_prepared_terminal_result",
            fake_terminal_result,
        )
        events: list[Any] = []

        async def collect(ev: Any) -> None:
            events.append(ev)

        deps = _build_deps(signal_event=collect)
        result = await execute_tool(
            _TEST_TOOL_PREPARE,
            {
                "resource_id": "file-1",
                "requested_outcome": "execute_if_approved",
            },
            deps,
        )

        assert result.ok
        assert result.data == {"successCount": 2}
        confirmation = next(
            event for event in events if isinstance(event, ConfirmationRequiredEvent)
        )
        assert confirmation.tool == _TEST_TOOL_PREPARED_EXECUTE
        assert confirmation.presentation == {
            "title": "Import 2 users",
            "fields": [
                {"label": "new", "value": 2},
                {"label": "exists", "value": 0},
            ],
            "warnings": [],
        }
        assert "server-only-token" not in repr(events)

        from app.db.session import AsyncSessionLocal
        from app.modules.ai.models.prepared_action import AiPreparedAction

        async with AsyncSessionLocal() as db:
            action = (
                await db.execute(
                    select(AiPreparedAction).where(
                        AiPreparedAction.confirmation_id == confirmation.confirmation_id
                    )
                )
            ).scalar_one()

        assert action.status == "pending_confirmation"
        assert action.prepare_tool_call_id is not None
        assert action.execute_tool_call_id == confirmation.tool_call_id
        assert action.execute_tool_name == _TEST_TOOL_PREPARED_EXECUTE
        assert action.frozen_args == {
            "preview_token": "server-only-token",
            "reason": "test import",
        }
        assert action.args_hash
        assert action.snapshot_hash
        assert action.user_id == 9001
        assert action.tenant_id == 77
        assert action.conversation_id == 100
        assert action.source_user_message_id == 101
        assert action.trace_id == "tr_test_001"
        assert action.expires_at == datetime.fromisoformat(
            confirmation.expires_at.replace("Z", "+00:00")
        )

    async def test_prepared_preview_rejects_missing_outcome(self) -> None:
        """省略执行意图字段不应被视为执行请求。"""
        _register_test_tools()
        events: list[Any] = []

        async def collect(ev: Any) -> None:
            events.append(ev)

        result = await execute_tool(
            _TEST_TOOL_PREPARE,
            {"resource_id": "file-1"},
            _build_deps(signal_event=collect),
        )

        assert not result.ok
        assert result.error_code == "AI_PREPARED_OUTCOME_REQUIRED"
        assert events == []

    async def test_high_risk_triggers_hitl_approved(self, monkeypatch) -> None:
        """high risk + count=None（无 dry_run_fn）→ HITL，mock hang 立即 APPROVED"""
        _register_test_tools()

        async def fake_hang(confirmation_id, *, timeout_sec=None):
            return ConfirmAction.APPROVED

        async def fake_terminal_result(confirmation_id):
            return ToolResult.success({"echo": {"x": 1}}), 1

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)
        monkeypatch.setattr(
            "app.modules.ai.agents.gateway.executor._load_prepared_terminal_result",
            fake_terminal_result,
        )

        events: list[Any] = []

        async def collect(ev: Any) -> None:
            events.append(ev)

        deps = _build_deps(signal_event=collect)
        result = await execute_tool(_TEST_TOOL_HIGH, {"x": 1}, deps)

        assert result.ok
        assert result.data == {"echo": {"x": 1}}

        types = [type(e).__name__ for e in events]
        assert "ToolCallStartedEvent" in types
        assert "ConfirmationRequiredEvent" in types
        assert "ToolCallResultEvent" in types

        # confirmation_required 在 tool_call_result 之前
        idx_confirm = types.index("ConfirmationRequiredEvent")
        idx_result = types.index("ToolCallResultEvent")
        assert idx_confirm < idx_result

    async def test_typed_dry_run_failure_stops_before_confirmation(
        self, monkeypatch
    ) -> None:
        _register_test_tools()
        hang = AsyncMock(return_value=ConfirmAction.REJECTED)
        monkeypatch.setattr(hitl_manager, "hang", hang)
        events: list[Any] = []

        async def collect(event: Any) -> None:
            events.append(event)

        deps = _build_deps(signal_event=collect)
        result = await execute_tool(_TEST_TOOL_DRY_RUN_DENIED, {}, deps)

        assert result.ok is False
        assert result.error_code == "AI_DATA_SCOPE_VIOLATION"
        hang.assert_not_awaited()
        assert not any(isinstance(event, ConfirmationRequiredEvent) for event in events)
        assert [type(event) for event in events] == [
            ToolCallStartedEvent,
            ToolCallResultEvent,
        ]
        assert events[-1].projection == ResultProjection()

        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT status, error_code FROM ai_operation_log "
                        "WHERE trace_id = 'tr_test_001' "
                        "AND tool_name = :tool_name "
                        "ORDER BY log_id DESC LIMIT 1"
                    ),
                    {"tool_name": _TEST_TOOL_DRY_RUN_DENIED},
                )
            ).one()

        assert row.status == "failed"
        assert row.error_code == "AI_DATA_SCOPE_VIOLATION"

    async def test_direct_hitl_persists_dry_run_exact_binding(
        self, monkeypatch
    ) -> None:
        _register_test_tools()

        async def fake_hang(confirmation_id, *, timeout_sec=None):
            return ConfirmAction.REJECTED

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)
        deps = _build_deps()
        result = await execute_tool(
            _TEST_TOOL_FREEZE_DIRECT,
            {"selector": "approved-name"},
            deps,
        )

        assert not result.ok

        from app.db.session import AsyncSessionLocal
        from app.modules.ai.models.prepared_action import AiPreparedAction

        async with AsyncSessionLocal() as db:
            action = (
                await db.execute(
                    select(AiPreparedAction).where(
                        AiPreparedAction.trace_id == deps.trace_id,
                        AiPreparedAction.execute_tool_name == _TEST_TOOL_FREEZE_DIRECT,
                    )
                )
            ).scalar_one()

        assert action.frozen_args == {"target_ids": [7001]}
        assert action.snapshot["argsHash"] == action.args_hash
        assert action.snapshot["business"] == {
            "targets": [{"id": "7001", "name": "approved"}]
        }

    async def test_direct_hitl_presents_canonical_execution_args(
        self, monkeypatch
    ) -> None:
        _register_test_tools()

        async def fake_hang(confirmation_id, *, timeout_sec=None):
            return ConfirmAction.REJECTED

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)
        events: list[Any] = []

        async def collect(event: Any) -> None:
            events.append(event)

        deps = _build_deps(signal_event=collect)
        result = await execute_tool(
            _TEST_TOOL_CANONICALIZE_DIRECT,
            {"scope": "SELF"},
            deps,
        )

        assert not result.ok
        confirmation = next(
            event for event in events if isinstance(event, ConfirmationRequiredEvent)
        )
        assert confirmation.presentation["fields"] == [
            {"label": "scope", "value": "SELF (5)", "rawValue": "5"},
            {"label": "affectedCount", "value": 1, "tone": "warning"},
        ]

        from app.db.session import AsyncSessionLocal
        from app.modules.ai.models.prepared_action import AiPreparedAction

        async with AsyncSessionLocal() as db:
            action = (
                await db.execute(
                    select(AiPreparedAction).where(
                        AiPreparedAction.trace_id == deps.trace_id,
                        AiPreparedAction.execute_tool_name
                        == _TEST_TOOL_CANONICALIZE_DIRECT,
                    )
                )
            ).scalar_one()

        assert action.frozen_args == {"scope": "5"}

    async def test_hitl_rejected(self, monkeypatch) -> None:
        """HITL reject → USER_REJECTED + log status=rejected"""
        _register_test_tools()

        async def fake_hang(confirmation_id, *, timeout_sec=None):
            return ConfirmAction.REJECTED

        async def fake_terminal_result(confirmation_id):
            return ToolResult.failure("USER_REJECTED", "cancelled"), 0

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)
        monkeypatch.setattr(
            "app.modules.ai.agents.gateway.executor._load_prepared_terminal_result",
            fake_terminal_result,
        )

        deps = _build_deps()
        result = await execute_tool(_TEST_TOOL_HIGH, {}, deps)

        assert not result.ok
        assert result.error_code == "USER_REJECTED"

    async def test_hitl_timeout(self, monkeypatch) -> None:
        """HITL 超时 → AI_HITL_EXPIRED + log status=expired"""
        _register_test_tools()

        async def fake_hang(confirmation_id, *, timeout_sec=None):
            raise TimeoutError("test timeout")

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)
        monkeypatch.setattr(
            "app.modules.ai.service.chat_run_service.chat_run_finalizer.finalize_prepared_action",
            AsyncMock(return_value=None),
        )

        deps = _build_deps()
        result = await execute_tool(_TEST_TOOL_HIGH, {}, deps)

        assert not result.ok
        assert result.error_code == "AI_HITL_EXPIRED"

        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            res = await db.execute(
                text(
                    "SELECT status FROM ai_operation_log "
                    "WHERE trace_id = 'tr_test_001' "
                    "ORDER BY log_id DESC LIMIT 1"
                )
            )
            row = res.first()
            assert row is not None
            assert row.status == "expired"

    async def test_confirmation_event_carries_safe_presentation(
        self, monkeypatch
    ) -> None:
        """confirmation_required exposes an action and safe DTO, never raw args."""
        _register_test_tools()

        async def fake_hang(confirmation_id, *, timeout_sec=None):
            return ConfirmAction.REJECTED

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)

        events: list[Any] = []

        async def collect(ev: Any) -> None:
            events.append(ev)

        deps = _build_deps(signal_event=collect)
        await execute_tool(_TEST_TOOL_HIGH, {"x": 1}, deps)

        confirm_events = [e for e in events if isinstance(e, ConfirmationRequiredEvent)]
        assert len(confirm_events) == 1
        ev = confirm_events[0]
        assert ev.tool == _TEST_TOOL_HIGH
        assert ev.confirmation_id
        assert ev.expires_at.endswith("Z")
        assert ev.action_id is not None
        assert ev.interaction_flow == "direct"
        assert ev.presentation == {
            "title": _TEST_TOOL_HIGH,
            "summary": "test high risk",
            "fields": [],
            "warnings": [],
        }
        assert not hasattr(ev, "args")


# ============ query_cache 写入 ============


class TestQueryCacheWrite:
    async def test_readonly_writes_query_cache(self) -> None:
        """只读工具成功后写入 ai:query_cache:<trace_id>。"""
        _register_test_tools()

        deps = _build_deps()
        # args 含 filters dict
        result = await execute_tool(
            _TEST_TOOL_READONLY,
            {"filters": {"status": "1", "user_gender": "2", "password": "leak"}},
            deps,
        )
        assert result.ok

        # 等待 fire-and-forget task 完成
        import asyncio

        await asyncio.sleep(0.1)

        from app.modules.ai.agents.hitl.query_cache import get_query_cache

        entry = await get_query_cache(redis_module.redis_client, deps.trace_id)
        assert entry is not None
        assert entry.tool_name == _TEST_TOOL_READONLY
        assert entry.module == "system/user"
        # filters 按 allowed_filters=("status","user_gender") 白名单过滤
        assert entry.filters == {"status": "1", "user_gender": "2"}
        # "password" 不在白名单，被剔除（防敏感字段进 cache）
        assert "password" not in entry.filters
        assert entry.user_id == 9001

    async def test_non_readonly_skips_query_cache(self) -> None:
        """readonly=False 不写 query_cache"""
        _register_test_tools()

        deps = _build_deps()
        await execute_tool(_TEST_TOOL_LOW, {"x": 1}, deps)

        import asyncio

        await asyncio.sleep(0.1)

        from app.modules.ai.agents.hitl.query_cache import get_query_cache

        entry = await get_query_cache(redis_module.redis_client, deps.trace_id)
        assert entry is None  # 没写


# ============ readonly tool affected_rows 门控 ============


class TestReadonlyAffectedRowsGate:
    """只读工具不展示 affected_rows，避免误导用户。

    user.count 返回 {"count": 42}，旧逻辑会推断 affected_rows=42 → 前端显示
    「已执行 · 230ms · 42 行」误导用户以为 42 行受影响。修法：readonly tool
    强制 affected_rows=None，前端 v-if 据此隐藏「N 行」后缀.
    """

    async def test_readonly_emits_null_affected_rows(self) -> None:
        """readonly tool 返回 {"count": 42} → emit affected_rows=None（非 42）"""
        _register_test_tools()

        events: list[Any] = []

        async def collect(ev: Any) -> None:
            events.append(ev)

        deps = _build_deps(signal_event=collect)
        result = await execute_tool(_TEST_TOOL_READONLY, {}, deps)
        assert result.ok
        # 业务层 ToolResult.data 仍保留原始 count（不破坏 LLM 可见的 data）
        assert result.data == {"count": 0}

        result_events = [e for e in events if isinstance(e, ToolCallResultEvent)]
        assert len(result_events) == 1
        # 关键断言：readonly tool 的 affected_rows 必须为 None（不是 0 也不是 42）
        assert result_events[0].affected_rows is None

    async def test_non_readonly_still_infers_affected_rows(self) -> None:
        """对照：非 readonly tool 返回 {"count": 5} 仍走推断（受影响行）"""
        _register_test_tools()

        events: list[Any] = []

        async def collect(ev: Any) -> None:
            events.append(ev)

        # _TEST_TOOL_LOW 不是 readonly，返回 {"echo": {...}}，无 count 信号 → None
        # 用动态注册一个带 count 的 non-readonly tool
        from app.modules.ai.agents.tools.decorator import ai_tool as _ai_tool
        from app.modules.ai.agents.tools.meta import AiToolMeta as _Meta
        from app.modules.ai.agents.tools.registry import ToolRegistry

        tool_name = "testint.non_readonly_count"

        @_ai_tool(
            _Meta(
                name=tool_name,
                agent="shared",
                summary="non-readonly with count",
                required_perms=(),
                risk="low",
                # readonly 默认 False
            )
        )
        async def _fn(ctx, **kwargs: Any) -> dict[str, Any]:
            return {"count": 7}

        try:
            deps = _build_deps(signal_event=collect)
            res = await execute_tool(tool_name, {}, deps)
            assert res.ok
            result_events = [e for e in events if isinstance(e, ToolCallResultEvent)]
            assert len(result_events) == 1
            # 非 readonly + result_data={"count": 7} → 推断 affected_rows=7
            assert result_events[0].affected_rows == 7
        finally:
            # 清理：避免污染其它测试
            ToolRegistry.get()._tools.pop(tool_name, None)  # noqa: SLF001


# ============ Redis 故障时 executor 降级 ============


class TestRedisDownGracefulDegrade:
    """Redis 故障时拒绝所有写操作并告警。

    Redis 是 quota / failures / hitl_manager / query_cache 的核心依赖。
    故障时应该优雅降级，不应让异常冒到用户层导致 500。
    """

    async def test_low_risk_tool_redis_down_internal_error(self) -> None:
        """low risk 工具不依赖 quota Redis（is_write_tool=False 跳过 L1/L2），
        但 dry_run / query_cache 仍可能用 Redis。low risk + 无 dry_run_fn 时
        Redis down 不影响（Redis 调用仅 query_cache 异步写入，失败静默）。
        """
        _register_test_tools()
        deps = _build_deps()
        # mock redis_client.incr 抛异常（虽然 low risk 不会调 incr）
        from app.modules.ai.agents.gateway import executor as exec_mod

        original = exec_mod.redis_client

        class FlakyRedis:
            async def incr(self, *_a, **_kw):
                raise ConnectionError("redis down")

            def __getattr__(self, name):
                # 其他方法走原 redis
                return getattr(original, name)

        exec_mod.redis_client = FlakyRedis()
        try:
            result = await execute_tool(_TEST_TOOL_LOW, {"msg": "hi"}, deps)
            # low risk 不依赖 quota，应正常成功
            assert result.ok is True
        finally:
            exec_mod.redis_client = original

    async def test_high_risk_tool_redis_down_failure(self) -> None:
        """high risk 写工具 Redis down → quota check 抛异常 → 应转 ToolResult.failure

        Redis 故障时写操作保守拒绝，不静默放过。
        executor.py 已加 RedisError 兜底，转 AI_REDIS_DOWN。
        """
        from redis.exceptions import ConnectionError as RedisConnectionError

        _register_test_tools()
        deps = _build_deps()
        from app.modules.ai.agents.gateway import executor as exec_mod

        original = exec_mod.redis_client

        class FlakyRedis:
            async def incr(self, *_a, **_kw):
                raise RedisConnectionError("redis down")

            async def get(self, *_a, **_kw):
                raise RedisConnectionError("redis down")

            def __getattr__(self, name):
                return getattr(original, name)

        exec_mod.redis_client = FlakyRedis()
        try:
            result = await execute_tool(_TEST_TOOL_HIGH, {"x": 1}, deps)
            assert not result.ok, "Redis down 时 high risk 写工具应拒绝（不静默放过）"
            assert result.error_code == "AI_REDIS_DOWN"
        finally:
            exec_mod.redis_client = original

    async def test_low_risk_failures_check_redis_down_rejected(self) -> None:
        """连续失败检查 Redis down → low risk 也应短路拒绝（保守降级）

        即使是 low risk，check_repeated_failure 走 Redis，故障时拒绝。
        安全检查失败时不允许任何工具继续执行。
        """
        from redis.exceptions import ConnectionError as RedisConnectionError

        _register_test_tools()
        deps = _build_deps()
        from app.modules.ai.agents.gateway import executor as exec_mod

        original = exec_mod.redis_client

        class FlakyRedis:
            async def get(self, *_a, **_kw):
                raise RedisConnectionError("redis down")

            def __getattr__(self, name):
                return getattr(original, name)

        exec_mod.redis_client = FlakyRedis()
        try:
            result = await execute_tool(_TEST_TOOL_LOW, {"msg": "hi"}, deps)
            assert not result.ok, "Redis down 时连续失败检查失败应拒绝"
            assert result.error_code == "AI_REDIS_DOWN"
        finally:
            exec_mod.redis_client = original


# ============ per-agent L2 叠加全局 L2 ============


class TestPerAgentQuota:
    """配置 agent.daily_quota_per_user 时叠加 per-agent L2。"""

    async def test_no_agent_quota_skips_per_agent_check(self) -> None:
        """agent.daily_quota_per_user=None → 不调 check_l2_agent_quota，key 不存在"""
        _register_test_tools()
        deps = _build_deps(agent_daily_quota=None, agent_code="shared")

        result = await execute_tool(_TEST_TOOL_LOW, {"msg": "hi"}, deps)
        assert result.ok, f"默认 agent 无专属额度应通过，got {result.error_code}"

        from datetime import UTC, datetime

        from app.core import redis as redis_module

        date_str = datetime.now(UTC).strftime("%Y%m%d")
        exists = await redis_module.redis_client.exists(
            f"ai:quota:9001:shared:{date_str}"
        )
        assert exists == 0  # per-agent key 未写

    async def test_agent_quota_under_limit_passes(self, monkeypatch) -> None:
        """agent.daily_quota_per_user=5 → 单次 high-risk tool 通过"""
        _register_test_tools()
        # 用 unique user_id 隔离避免污染
        user = MagicMock()
        user.user_id = 9004
        agent = MagicMock()
        agent.code = "shared"
        agent.enabled = True
        agent.daily_quota_per_user = 5

        deps = ChatDeps(
            user=user,
            perms={"*"},
            db=MagicMock(),
            data_scope=DataScopeContext(
                accessible_dept_ids=None, accessible_user_scope=None, filters=[]
            ),
            agent=agent,
            trace_id="tr_test_agent_pass",
            conversation_id=400,
            source_user_message_id=401,
            resolved_model_id=7001,
            resolved_provider_id=8001,
        )

        # low risk tool 也会触发 quota 检查吗？不会——is_write_tool=False。
        # 用 _TEST_TOOL_LOW（risk=low）测不出 per-agent L2，需要 high risk。
        # 但 high risk + dry_run_count=None → HITL 路径，等 confirm → expired。
        # 解决：换用 _TEST_TOOL_HIGH 但工具内部已 self-contained，HITL expired 是预期。
        # 这里只验证 per-agent key 在 quota check 阶段已被写入（即使最终 HITL expired）。
        async def expire_immediately(confirmation_id, *, timeout_sec=None):
            raise TimeoutError("quota test does not wait for human input")

        monkeypatch.setattr(hitl_manager, "hang", expire_immediately)
        monkeypatch.setattr(
            "app.modules.ai.service.chat_run_service.chat_run_finalizer.finalize_prepared_action",
            AsyncMock(return_value=None),
        )
        await execute_tool(_TEST_TOOL_HIGH, {"x": 1}, deps)

        from datetime import UTC, datetime

        from app.core import redis as redis_module

        date_str = datetime.now(UTC).strftime("%Y%m%d")
        agent_count = int(
            await redis_module.redis_client.get(f"ai:quota:9004:shared:{date_str}") or 0
        )
        assert agent_count == 1, f"per-agent L2 应 INCR 1 次，got {agent_count}"

    async def test_agent_quota_exhausted_after_limit(self) -> None:
        """agent.daily_quota_per_user=1 → 第 2 次 high risk 调用 per-agent L2 拦截

        关键：用 low-risk tool 测不出（is_write_tool=False）。
        改用直接调 check_l2_agent_quota（已在 test_quota_failures.py 覆盖），
        此处验证 executor 不会因 per-agent 已满而错误地让 low-risk tool 也失败。
        """
        _register_test_tools()
        user = MagicMock()
        user.user_id = 9005
        agent = MagicMock()
        agent.code = "shared"
        agent.enabled = True
        agent.daily_quota_per_user = 1

        deps = ChatDeps(
            user=user,
            perms={"*"},
            db=MagicMock(),
            data_scope=DataScopeContext(
                accessible_dept_ids=None, accessible_user_scope=None, filters=[]
            ),
            agent=agent,
            trace_id="tr_test_agent_full",
            conversation_id=500,
        )

        # 预热 per-agent L2 到 limit（直接调底层函数）
        from app.core import redis as redis_module
        from app.modules.ai.agents.gateway import check_l2_agent_quota

        await check_l2_agent_quota(redis_module.redis_client, 9005, "shared", limit=1)

        # 现在 per-agent 已满，high-risk tool 应被拦
        result = await execute_tool(_TEST_TOOL_HIGH, {"x": 1}, deps)
        assert not result.ok
        assert result.error_code == "AI_DAILY_QUOTA_EXHAUSTED"
