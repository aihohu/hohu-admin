"""spec §12.2 鉴权矩阵端到端覆盖

9 个 case 覆盖（#7 #10 留 Phase 4）：

| # | 场景 | 测试方式 | 预期 |
|---|---|---|---|
| 1 | 低风险查询 | execute_tool | autonomous |
| 2 | 高风险单行修改（dry_run=1） | execute_tool | autonomous |
| 3 | 高风险多行修改（dry_run=2） | execute_tool | HITL |
| 4 | 破坏性操作 | execute_tool | HITL |
| 5 | 无权限 | compute_available_tools | tool 不可见 |
| 6 | data_scope 越界 | execute_tool | AI_DATA_SCOPE_VIOLATION |
| 7 | 改权限码 + 非超管 | — | AI_SUPER_ADMIN_REQUIRED（skip: Phase 4） |
| 8 | hitl_always=True | execute_tool | 强制 HITL |
| 9 | 日配额超限 | execute_tool | AI_DAILY_QUOTA_EXHAUSTED |
| 10 | Prompt injection 命中 | — | 强制 HITL（skip: Phase 4） |
| 11 | LLM 幻觉调不存在 tool | execute_tool | AI_TOOL_NOT_FOUND |

断言约定（spec §12.2）：
- autonomous / HITL 类用例 → 调 execute_tool，断言事件序列含/不含 confirmation_required
- 错误码类用例 → 断言 ToolResult.error_code
- tool 不可见类用例 → 断言 compute_available_tools 不含
"""

# ruff: noqa: ARG001, PLC0415

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.asyncio as aioredis
from sqlalchemy import Select, literal, select, text

from app.core import redis as redis_module
from app.core.config import settings
from app.modules.ai.agents.gateway.executor import execute_tool
from app.modules.ai.agents.gateway.quota import _KEY_L2, DEFAULT_L2_DAILY_QUOTA
from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.agents.gateway.targets import ensure_targets_in_scope
from app.modules.ai.agents.hitl.constants import ConfirmAction
from app.modules.ai.agents.hitl.events import (
    ConfirmationRequiredEvent,
    ToolCallResultEvent,
    ToolCallStartedEvent,
)
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.agents.tools.registry import ToolRegistry, compute_available_tools
from app.modules.ai.core.context import ChatDeps, DataScopeContext


@pytest.fixture(autouse=True)
async def clean_env(monkeypatch):
    """每测试清 Redis + reset hitl_manager + 清本轮测试日志。"""
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
                text("DELETE FROM ai_prepared_action WHERE trace_id = 'tr_authz_test'")
            )
            await db.execute(
                text("DELETE FROM ai_operation_log WHERE trace_id = 'tr_authz_test'")
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
                text("DELETE FROM ai_prepared_action WHERE trace_id = 'tr_authz_test'")
            )
            await db.execute(
                text("DELETE FROM ai_operation_log WHERE trace_id = 'tr_authz_test'")
            )

    from app.db.session import engine

    try:
        await engine.dispose()
    except RuntimeError as e:
        if "Event loop is closed" not in str(e):
            raise

    redis_module.redis_pool = original_pool
    redis_module.redis_client = original_client
    exec_mod.redis_client = original_client


# ============ 测试 tool（testz.* 前缀避免与 test_executor_integration 冲突） ============

_T_LOW = "testz.low_lookup"
_T_HIGH_SINGLE = "testz.high_single"
_T_HIGH_MULTI = "testz.high_multi"
_T_DESTRUCTIVE = "testz.destructive_op"
_T_PERMED = "testz.perm_required"
_T_SCOPE_VIOLATION = "testz.scope_check"
_T_HITL_ALWAYS = "testz.hitl_always"
_T_QUOTA_TEST = "testz.write_op"
_T_SUPER_ADMIN = "testz.super_admin_only"

_AGENT_CODE = "shared"
_TOOLS_REGISTERED = False


def _register_test_tools() -> None:
    """注册鉴权矩阵测试 tool（一次性，全局 Registry）"""
    global _TOOLS_REGISTERED
    if _TOOLS_REGISTERED:
        return
    _TOOLS_REGISTERED = True

    @ai_tool(
        AiToolMeta(
            name=_T_LOW,
            agent=_AGENT_CODE,
            summary="testz low risk lookup",
            required_perms=(),
            risk="low",
        )
    )
    async def _low_fn(ctx, **kw: Any) -> dict[str, Any]:
        return {"count": 1}

    @ai_tool(
        AiToolMeta(
            name=_T_HIGH_SINGLE,
            agent=_AGENT_CODE,
            summary="testz high risk single row",
            required_perms=(),
            risk="high",
            dry_run_supported=True,
        )
    )
    async def _high_single_fn(ctx, **kw: Any) -> dict[str, Any]:
        return {"affected_count": 1}

    @ai_tool(
        AiToolMeta(
            name=_T_HIGH_MULTI,
            agent=_AGENT_CODE,
            summary="testz high risk multi row",
            required_perms=(),
            risk="high",
            dry_run_supported=True,
        )
    )
    async def _high_multi_fn(ctx, **kw: Any) -> dict[str, Any]:
        return {"affected_count": 2}

    @ai_tool(
        AiToolMeta(
            name=_T_DESTRUCTIVE,
            agent=_AGENT_CODE,
            summary="testz destructive op",
            required_perms=(),
            risk="destructive",
        )
    )
    async def _destructive_fn(ctx, **kw: Any) -> dict[str, Any]:
        return {"affected_count": 1}

    @ai_tool(
        AiToolMeta(
            name=_T_PERMED,
            agent=_AGENT_CODE,
            summary="testz perm required tool",
            required_perms=("testz:special_perm",),
            risk="low",
        )
    )
    async def _permed_fn(ctx, **kw: Any) -> dict[str, Any]:
        return {"ok": True}

    @ai_tool(
        AiToolMeta(
            name=_T_SCOPE_VIOLATION,
            agent=_AGENT_CODE,
            summary="testz tool with scope check",
            required_perms=(),
            risk="low",
        )
    )
    async def _scope_fn(ctx, *, user_ids: list[int]) -> dict[str, Any]:
        await ensure_targets_in_scope(ctx, user_ids=user_ids)
        return {"affected_count": len(user_ids)}

    @ai_tool(
        AiToolMeta(
            name=_T_HITL_ALWAYS,
            agent=_AGENT_CODE,
            summary="testz hitl_always tool",
            required_perms=(),
            risk="low",
            hitl_always=True,
        )
    )
    async def _hitl_always_fn(ctx, **kw: Any) -> dict[str, Any]:
        return {"ok": True}

    @ai_tool(
        AiToolMeta(
            name=_T_QUOTA_TEST,
            agent=_AGENT_CODE,
            summary="testz write tool for quota test",
            required_perms=(),
            risk="high",
        )
    )
    async def _quota_fn(ctx, **kw: Any) -> dict[str, Any]:
        return {"ok": True}

    @ai_tool(
        AiToolMeta(
            name=_T_SUPER_ADMIN,
            agent=_AGENT_CODE,
            summary="testz super_admin_only tool",
            required_perms=(),
            risk="low",
            super_admin_only=True,
        )
    )
    async def _super_admin_fn(ctx, **kw: Any) -> dict[str, Any]:
        return {"ok": True}

    # 注入 dry_run_fn（用于 #2 #3）
    registry = ToolRegistry.get()

    async def _dry_run_single(ctx, **kw: Any) -> Any:
        from app.modules.ai.agents.hitl.constants import DryRunResult

        return DryRunResult(ok=True, count=1, reason="将影响 1 行")

    async def _dry_run_multi(ctx, **kw: Any) -> Any:
        from app.modules.ai.agents.hitl.constants import DryRunResult

        return DryRunResult(ok=True, count=2, reason="将影响 2 行")

    registry.set_dry_run_fn(_T_HIGH_SINGLE, _dry_run_single)
    registry.set_dry_run_fn(_T_HIGH_MULTI, _dry_run_multi)


def _build_deps(
    *,
    perms: set[str] | None = None,
    accessible_user_scope: Select[tuple[int]] | None = None,
    signal_event=None,
) -> ChatDeps:
    """构造测试 ChatDeps"""
    user = MagicMock()
    user.user_id = 9001

    data_scope = DataScopeContext(
        accessible_dept_ids=None,
        accessible_user_scope=accessible_user_scope,
        filters=[],
    )
    agent = MagicMock()
    agent.code = _AGENT_CODE
    agent.daily_quota_per_user = None  # v1.5+ SR-16: 默认未配专属额度

    return ChatDeps(
        user=user,
        perms=perms if perms is not None else {"*"},
        db=MagicMock(),
        data_scope=data_scope,
        agent=agent,
        trace_id="tr_authz_test",
        conversation_id=200,
        source_user_message_id=201,
        signal_event=signal_event,
    )


def _mock_durable_success(monkeypatch) -> None:  # noqa: ANN001
    async def fake_terminal_result(confirmation_id):  # noqa: ARG001
        return ToolResult.success({"approved": True}), 1

    monkeypatch.setattr(
        "app.modules.ai.agents.gateway.executor._load_prepared_terminal_result",
        fake_terminal_result,
    )


async def _execute_and_collect(
    name: str, args: dict, deps: ChatDeps
) -> tuple[Any, list]:
    """执行 tool 并收集 SSE 事件 + 返回 ToolResult

    Returns:
        (result, events) — short-circuit 路径（tool not found / perm denied /
        quota exhausted）只返回 result 不 emit 事件；正常路径含 started + result
        事件，HITL 路径含 confirmation_required
    """
    events: list = []

    async def collect(ev) -> None:
        events.append(ev)

    deps.signal_event = collect
    result = await execute_tool(name, args, deps)
    return result, events


def _has_confirmation(events: list) -> bool:
    return any(isinstance(e, ConfirmationRequiredEvent) for e in events)


# ============ 矩阵 #1: 低风险查询 → autonomous ============


class TestCase1LowRiskAutonomous:
    """#1: risk=low + perm ✅ + data_scope in → autonomous（无 confirmation_required）"""

    async def test_low_risk_is_autonomous(self) -> None:
        _register_test_tools()
        deps = _build_deps()
        result, events = await _execute_and_collect(_T_LOW, {}, deps)

        # 无 confirmation_required
        assert not _has_confirmation(events), "low risk 不应触发 HITL"
        # 含 started + result
        assert any(isinstance(e, ToolCallStartedEvent) for e in events)
        result_events = [e for e in events if isinstance(e, ToolCallResultEvent)]
        assert len(result_events) == 1
        assert result_events[0].ok is True
        assert result.ok is True


# ============ 矩阵 #2: 高风险单行修改 → autonomous（dry_run_count=1） ============


class TestCase2HighRiskSingleRowAutonomous:
    """#2: risk=high + dry_run_count=1 → §5.3 矩阵 autonomous"""

    async def test_high_risk_single_row_is_autonomous(self) -> None:
        _register_test_tools()
        deps = _build_deps()
        result, events = await _execute_and_collect(_T_HIGH_SINGLE, {}, deps)

        assert not _has_confirmation(events), "high + dry_run=1 不应触发 HITL"
        result_events = [e for e in events if isinstance(e, ToolCallResultEvent)]
        assert result_events[0].ok is True
        assert result.ok is True


# ============ 矩阵 #3: 高风险多行修改 → HITL（dry_run_count=2） ============


class TestCase3HighRiskMultiRowHitl:
    """#3: risk=high + dry_run_count=2 → §5.3 矩阵 HITL"""

    async def test_high_risk_multi_row_triggers_hitl(self, monkeypatch) -> None:
        _register_test_tools()

        # mock hitl_manager.hang 立即 APPROVED（避免真的等 5 分钟）
        async def fake_hang(confirmation_id, *, timeout_sec=None):
            return ConfirmAction.APPROVED

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)
        _mock_durable_success(monkeypatch)

        deps = _build_deps()
        result, events = await _execute_and_collect(_T_HIGH_MULTI, {}, deps)

        assert _has_confirmation(events), "high + dry_run=2 必须触发 HITL"
        result_events = [e for e in events if isinstance(e, ToolCallResultEvent)]
        assert result_events[0].ok is True
        assert result.ok is True


# ============ 矩阵 #4: 破坏性操作 → HITL ============


class TestCase4DestructiveHitl:
    """#4: risk=destructive → §5.3 矩阵 HITL（无视 dry_run_count）"""

    async def test_destructive_always_triggers_hitl(self, monkeypatch) -> None:
        _register_test_tools()

        async def fake_hang(confirmation_id, *, timeout_sec=None):
            return ConfirmAction.APPROVED

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)
        _mock_durable_success(monkeypatch)

        deps = _build_deps()
        result, events = await _execute_and_collect(_T_DESTRUCTIVE, {}, deps)

        assert _has_confirmation(events), "destructive 必须触发 HITL"
        result_events = [e for e in events if isinstance(e, ToolCallResultEvent)]
        assert result_events[0].ok is True
        assert result.ok is True


# ============ 矩阵 #5: 无权限 → tool 不可见（compute_available_tools 过滤） ============


class TestCase5ToolInvisible:
    """#5: 用户 perms 不含 tool.required_perms → compute_available_tools 不返回该 tool"""

    async def test_perm_filter_hides_tool(self) -> None:
        _register_test_tools()

        # 用户只有通配 perms，没有 testz:special_perm
        visible = compute_available_tools(set(), _AGENT_CODE)
        names = {t.meta.name for t in visible}
        assert _T_PERMED not in names, "缺 perm 的 tool 不应在可见列表"
        # 但其他无 perm 要求的 tool 应该可见
        assert _T_LOW in names

    async def test_with_perm_tool_becomes_visible(self) -> None:
        _register_test_tools()
        visible = compute_available_tools({"testz:special_perm"}, _AGENT_CODE)
        names = {t.meta.name for t in visible}
        assert _T_PERMED in names


# ============ 矩阵 #6: data_scope 越界 → AI_DATA_SCOPE_VIOLATION ============


class TestCase6DataScopeViolation:
    """#6: tool fn 内 ensure_targets_in_scope 抛 AI_DATA_SCOPE_VIOLATION"""

    async def test_target_out_of_scope_raises(self) -> None:
        _register_test_tools()
        # 用 literal 构造 scope：返 100, 200（不依赖 DB User 表数据）
        scope = (
            select(literal(100).label("user_id"))
            .union_all(select(literal(200).label("user_id")))
            .subquery()
            .select()
        )
        deps = _build_deps(accessible_user_scope=scope)

        result, events = await _execute_and_collect(
            _T_SCOPE_VIOLATION, {"user_ids": [100, 999]}, deps
        )
        result_events = [e for e in events if isinstance(e, ToolCallResultEvent)]
        assert len(result_events) == 1
        assert result_events[0].ok is False
        assert result_events[0].error_code == "AI_DATA_SCOPE_VIOLATION"
        assert result.ok is False
        assert result.error_code == "AI_DATA_SCOPE_VIOLATION"

    async def test_target_in_scope_passes(self) -> None:
        _register_test_tools()
        scope = (
            select(literal(100).label("user_id"))
            .union_all(select(literal(200).label("user_id")))
            .subquery()
            .select()
        )
        deps = _build_deps(accessible_user_scope=scope)
        result, events = await _execute_and_collect(
            _T_SCOPE_VIOLATION, {"user_ids": [100, 200]}, deps
        )
        result_events = [e for e in events if isinstance(e, ToolCallResultEvent)]
        assert result_events[0].ok is True
        assert result.ok is True


# ============ 矩阵 #7: 改权限码 + 非超管 → AI_SUPER_ADMIN_REQUIRED ============


class TestCase7SuperAdminGate:
    """#7: super_admin_only=True + 非超管 → AI_SUPER_ADMIN_REQUIRED（short-circuit）

    spec §11.2: 改权限码 / 删 super_admin 账号等高权限操作仅超管可执行。
    短路在 perm check 之后、dry_run 之前，不走 HITL 也不进风险分级。
    """

    async def test_non_super_admin_rejected(self) -> None:
        _register_test_tools()
        deps = _build_deps()  # MagicMock user, is_super_admin 返回 False
        result, events = await _execute_and_collect(_T_SUPER_ADMIN, {}, deps)
        # short-circuit：无事件
        assert events == []
        assert result.ok is False
        assert result.error_code == "AI_SUPER_ADMIN_REQUIRED"

    async def test_super_admin_passes(self) -> None:
        """超管（user_name='admin'）可调用 super_admin_only tool"""
        _register_test_tools()
        # 构造 super admin user：user_name='admin' 触发 is_super_admin 第一条规则
        deps = _build_deps()
        deps.user.user_name = "admin"
        result, events = await _execute_and_collect(_T_SUPER_ADMIN, {}, deps)
        assert result.ok is True
        # 走完整流程，emit started + result
        assert any(isinstance(e, ToolCallStartedEvent) for e in events)


# ============ 矩阵 #8: hitl_always=True → 强制 HITL ============


class TestCase8HitlAlwaysForcesHitl:
    """#8: low risk + hitl_always=True → §5.3 优先级 1 强制 HITL"""

    async def test_hitl_always_forces_hitl(self, monkeypatch) -> None:
        _register_test_tools()

        async def fake_hang(confirmation_id, *, timeout_sec=None):
            return ConfirmAction.APPROVED

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)
        _mock_durable_success(monkeypatch)

        deps = _build_deps()
        result, events = await _execute_and_collect(_T_HITL_ALWAYS, {}, deps)

        assert _has_confirmation(events), "hitl_always=True 必须触发 HITL"
        result_events = [e for e in events if isinstance(e, ToolCallResultEvent)]
        assert result_events[0].ok is True
        assert result.ok is True


# ============ 矩阵 #9: 日配额超限 → AI_DAILY_QUOTA_EXHAUSTED ============


class TestCase9DailyQuotaExhausted:
    """#9: 写 tool 当日配额耗尽 → AI_DAILY_QUOTA_EXHAUSTED

    short-circuit 路径：L2 检查失败时不 emit 事件，直接 return ToolResult.failure
    """

    async def test_quota_exhausted_rejected(self) -> None:
        _register_test_tools()

        # 预填 Redis 配额到 limit（执行前 INCR 即超限）
        # 修订 S-8：L2 date key 用 UTC compact 格式 YYYYMMDD（不再是 ISO YYYY-MM-DD）
        today = datetime.now(UTC).strftime("%Y%m%d")
        key = _KEY_L2.format(user_id=9001, date=today)
        await redis_module.redis_client.set(key, DEFAULT_L2_DAILY_QUOTA)

        deps = _build_deps()
        result, events = await _execute_and_collect(_T_QUOTA_TEST, {}, deps)

        # short-circuit：无事件
        assert events == []
        assert result.ok is False
        assert result.error_code == "AI_DAILY_QUOTA_EXHAUSTED"


# ============ 矩阵 #10: Prompt injection 命中 → 强制 HITL ============


class TestCase10InjectionDetector:
    """#10: 命中 prompt injection pattern → 强制 HITL（spec §11.1 降级而非拒绝）

    spec §11.1: 注入命中后**降级**到强制 HITL，不直接拒绝。这样：
      - 真用户偶然命中（误报）仍能通过 HITL 完成
      - 攻击者必须经过 HITL（被审计 + 人工把关）
    """

    async def test_injection_hit_forces_hitl(self, monkeypatch) -> None:
        _register_test_tools()

        # mock hitl_manager.hang 立即 APPROVED
        async def fake_hang(confirmation_id, *, timeout_sec=None):
            return ConfirmAction.APPROVED

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)
        _mock_durable_success(monkeypatch)

        # injection_hit=True 触发强制 HITL（即使 risk=low）
        deps = _build_deps()
        deps.injection_hit = True
        result, events = await _execute_and_collect(_T_LOW, {}, deps)

        # low risk 工具因 injection_hit 被强制 HITL
        assert _has_confirmation(events), (
            "injection_hit=True 必须强制 HITL（即使 risk=low）"
        )
        result_events = [e for e in events if isinstance(e, ToolCallResultEvent)]
        assert result_events[0].ok is True
        assert result.ok is True

        # §11.1: injection_hit=True 时 ai_operation_log 落 is_security_event=True
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            res = await db.execute(
                text(
                    "SELECT is_security_event, event_type FROM ai_operation_log "
                    "WHERE trace_id = 'tr_authz_test' "
                    "ORDER BY started_at DESC LIMIT 1"
                )
            )
            row = res.first()
        assert row is not None, "ai_operation_log 行必须写入"
        assert row.is_security_event is True
        assert row.event_type == "injection_pattern_matched"

    async def test_no_injection_low_risk_autonomous(self) -> None:
        """对照：同 tool 同 args，injection_hit=False → autonomous（无 confirmation）"""
        _register_test_tools()
        deps = _build_deps()
        # injection_hit 默认 False
        result, events = await _execute_and_collect(_T_LOW, {}, deps)
        assert not _has_confirmation(events), "无注入命中时 low risk 应 autonomous"


# ============ 矩阵 #11: LLM 幻觉调不存在 tool → AI_TOOL_NOT_FOUND ============


class TestCase11ToolNotFound:
    """#11: LLM 调用未注册 tool → AI_TOOL_NOT_FOUND

    short-circuit 路径：tool 不存在时不 emit 事件，直接 return ToolResult.failure
    """

    async def test_nonexistent_tool_returns_not_found(self) -> None:
        _register_test_tools()
        deps = _build_deps()
        result, events = await _execute_and_collect(
            "testz.does_not_exist", {"x": 1}, deps
        )

        # short-circuit：无事件
        assert events == []
        assert result.ok is False
        assert result.error_code == "AI_TOOL_NOT_FOUND"
