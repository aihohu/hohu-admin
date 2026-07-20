"""execute_tool 集成测试 — Phase 3.2 HITL + 流式协议 + 审计

按 spec §3 / §6 / §8.2 验证：
  - tool not found / perm denied 短路返回 ToolResult.failure
  - autonomous 流：emit tool_call_started + tool_call_result + 写 ai_operation_log
  - HITL 流（mock hitl_manager.hang）：emit confirmation_required + 接受 wake
"""

# ruff: noqa: ARG001, PLC0415

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
import redis.asyncio as aioredis
from sqlalchemy import text

from app.core import redis as redis_module
from app.core.config import settings
from app.modules.ai.agents.gateway.executor import execute_tool
from app.modules.ai.agents.hitl.constants import ConfirmAction
from app.modules.ai.agents.hitl.events import (
    ConfirmationRequiredEvent,
    ToolCallResultEvent,
    ToolCallStartedEvent,
)
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.agents.tools.decorator import ai_tool
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.core.context import ChatDeps, DataScopeContext


@pytest.fixture(autouse=True)
async def clean_env():
    """每个测试前：重建 redis_client + 清 Redis + reset hitl_manager + 清本轮测试日志。

    ai_operation_log 不能用 TRUNCATE（会清掉生产 AI 审计日志）。所有测试代码
    通过 _build_deps 写入的行 trace_id 都是 'tr_test_001'，只 DELETE 这部分
    精准清理，生产数据保持不动。
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

    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(
                text("DELETE FROM ai_operation_log WHERE trace_id = 'tr_test_001'")
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
                text("DELETE FROM ai_operation_log WHERE trace_id = 'tr_test_001'")
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
    async def _readonly_list(ctx, **kwargs: Any) -> dict[str, Any]:
        return {"count": 0}


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
    agent.daily_quota_per_user = agent_daily_quota  # v1.5+ SR-16

    return ChatDeps(
        user=user,
        perms=perms if perms is not None else {"*"},
        db=MagicMock(),
        data_scope=data_scope,
        agent=agent,
        trace_id="tr_test_001",
        conversation_id=100,
        signal_event=signal_event,
    )


# ============ _infer_affected_rows helper ============


class TestInferAffectedRows:
    """spec §8.1: result 卡片「N 行」尾部的来源规则"""

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


class TestShortCircuit:
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


# ============ autonomous 流 ============


class TestAutonomousFlow:
    async def test_emits_started_and_result(self) -> None:
        """spec §8.1: autonomous 流 emit tool_call_started + tool_call_result

        spec §8.1（更新）: started 透传 risk；result 含 duration_ms + affected_rows
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
        """spec §9.1: 每次 tool 调用写一行 ai_operation_log（autonomous → success）"""
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


# ============ HITL 流（mock hitl_manager.hang 立即返回） ============


class TestHitlFlow:
    async def test_high_risk_triggers_hitl_approved(self, monkeypatch) -> None:
        """high risk + count=None（无 dry_run_fn）→ HITL，mock hang 立即 APPROVED"""
        _register_test_tools()

        async def fake_hang(confirmation_id, *, timeout_sec=None):
            return ConfirmAction.APPROVED

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)

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

    async def test_hitl_rejected(self, monkeypatch) -> None:
        """HITL reject → USER_REJECTED + log status=rejected"""
        _register_test_tools()

        async def fake_hang(confirmation_id, *, timeout_sec=None):
            return ConfirmAction.REJECTED

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)

        deps = _build_deps()
        result = await execute_tool(_TEST_TOOL_HIGH, {}, deps)

        assert not result.ok
        assert result.error_code == "USER_REJECTED"

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
            assert row.status == "rejected"

    async def test_hitl_timeout(self, monkeypatch) -> None:
        """HITL 超时 → AI_HITL_EXPIRED + log status=expired"""
        _register_test_tools()

        async def fake_hang(confirmation_id, *, timeout_sec=None):
            raise TimeoutError("test timeout")

        monkeypatch.setattr(hitl_manager, "hang", fake_hang)

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

    async def test_confirmation_event_carries_payload(self, monkeypatch) -> None:
        """confirmation_required 事件含 confirmation_id / expires_at / args"""
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
        assert ev.args == {"x": 1}


# ============ query_cache 写入（spec §8.7） ============


class TestQueryCacheWrite:
    async def test_readonly_writes_query_cache(self) -> None:
        """spec §8.7: readonly tool 成功后写 ai:query_cache:<trace_id>"""
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


# ============ 边界：Redis down 时 executor 降级（spec §2.6） ============


class TestRedisDownGracefulDegrade:
    """spec §2.6: Redis 故障 → 所有写操作拒绝 + 告警

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

        spec §2.6: Redis 故障时写操作拒绝（保守降级，不静默放过）。
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
        spec §2.6: 安全检查失败时不放过任何 tool。
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


# ============ v1.5+ SR-16: per-agent L2 叠加全局 L2 ============


class TestPerAgentQuota:
    """spec §6.4 SR-16：agent.daily_quota_per_user 非 None 时叠加 per-agent L2"""

    async def test_no_agent_quota_skips_per_agent_check(self) -> None:
        """agent.daily_quota_per_user=None → 不调 check_l2_agent_quota，key 不存在"""
        _register_test_tools()
        deps = _build_deps(agent_daily_quota=None, agent_code="user_mgmt")

        result = await execute_tool(_TEST_TOOL_LOW, {"msg": "hi"}, deps)
        assert result.ok, f"默认 agent 无专属额度应通过，got {result.error_code}"

        from datetime import UTC, datetime

        from app.core import redis as redis_module

        date_str = datetime.now(UTC).strftime("%Y%m%d")
        exists = await redis_module.redis_client.exists(
            f"ai:quota:9001:user_mgmt:{date_str}"
        )
        assert exists == 0  # per-agent key 未写

    async def test_agent_quota_under_limit_passes(self) -> None:
        """agent.daily_quota_per_user=5 → 单次 high-risk tool 通过"""
        _register_test_tools()
        # 用 unique user_id 隔离避免污染
        user = MagicMock()
        user.user_id = 9004
        agent = MagicMock()
        agent.code = "test_agent_pass"
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
        )

        # low risk tool 也会触发 quota 检查吗？不会——is_write_tool=False。
        # 用 _TEST_TOOL_LOW（risk=low）测不出 per-agent L2，需要 high risk。
        # 但 high risk + dry_run_count=None → HITL 路径，等 confirm → expired。
        # 解决：换用 _TEST_TOOL_HIGH 但工具内部已 self-contained，HITL expired 是预期。
        # 这里只验证 per-agent key 在 quota check 阶段已被写入（即使最终 HITL expired）。
        await execute_tool(_TEST_TOOL_HIGH, {"x": 1}, deps)

        from datetime import UTC, datetime

        from app.core import redis as redis_module

        date_str = datetime.now(UTC).strftime("%Y%m%d")
        agent_count = int(
            await redis_module.redis_client.get(
                f"ai:quota:9004:test_agent_pass:{date_str}"
            )
            or 0
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
        agent.code = "test_agent_full"
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

        await check_l2_agent_quota(
            redis_module.redis_client, 9005, "test_agent_full", limit=1
        )

        # 现在 per-agent 已满，high-risk tool 应被拦
        result = await execute_tool(_TEST_TOOL_HIGH, {"x": 1}, deps)
        assert not result.ok
        assert result.error_code == "AI_DAILY_QUOTA_EXHAUSTED"
