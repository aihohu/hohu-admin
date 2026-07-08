"""容量三层 + 连续失败兜底 单元测试

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §6.4 / §6.5。

Redis 用真实 redis_client（ai/conftest.py 的 db_session fixture 已 reset），
不用 mock（mock Redis INCR + EXPIRE 复杂且易错）。
"""

# ruff: noqa: ARG001, PLC0415

import asyncio

import pytest

from app.core import redis as redis_module
from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.gateway import (
    check_l1_rate_limit,
    check_l2_daily_quota,
    check_repeated_failure,
    clear_failures,
    compute_args_hash,
    is_write_tool,
    record_failure,
    with_l3_timeout,
)
from app.modules.ai.agents.tools import AiToolMeta

# ============ is_write_tool ============


def _meta(risk: str = "low", hitl_always: bool = False) -> AiToolMeta:
    return AiToolMeta(
        name="x.y",
        agent="a",
        summary="s",
        required_perms=("p",),
        risk=risk,  # type: ignore[arg-type]
        hitl_always=hitl_always,
    )


class TestIsWriteTool:
    def test_low_is_not_write(self) -> None:
        """spec §6.4: risk=low 不计入 L1/L2"""
        assert is_write_tool(_meta(risk="low")) is False

    def test_high_is_write(self) -> None:
        assert is_write_tool(_meta(risk="high")) is True

    def test_destructive_is_write(self) -> None:
        assert is_write_tool(_meta(risk="destructive")) is True

    def test_hitl_always_is_write(self) -> None:
        """spec §6.4: hitl_always=True 即使 risk=low 也算写"""
        assert is_write_tool(_meta(risk="low", hitl_always=True)) is True


# ============ L1 用户写速率 ============


@pytest.fixture(autouse=True)
async def clean_redis_rate_keys():
    """每个测试前后清理 L1/L2/failures 相关 key + 重建 redis_client（绑新 loop）

    teardown 时还原原 redis_module 引用，避免干扰 marketplace / system 等其它
    测试套件（它们持有 redis_module.redis_client 引用，需被各自 fixture 重置）
    """
    import redis.asyncio as aioredis

    from app.core import redis as redis_module
    from app.core.config import settings

    # 保存原引用（teardown 时还原）
    original_pool = redis_module.redis_pool
    original_client = redis_module.redis_client

    # 重建 redis 客户端（每个测试新 event loop）
    redis_module.redis_pool = aioredis.ConnectionPool.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20,
        socket_timeout=5,
        socket_connect_timeout=5,
        retry_on_timeout=True,
    )
    redis_module.redis_client = aioredis.Redis(connection_pool=redis_module.redis_pool)

    # 同步刷新 executor 内的 redis_client 引用
    from app.modules.ai.agents.gateway import executor as exec_mod

    exec_mod.redis_client = redis_module.redis_client

    keys = await redis_module.redis_client.keys("ai:write:*")
    keys += await redis_module.redis_client.keys("ai:quota:*")
    keys += await redis_module.redis_client.keys("ai:failures:*")
    if keys:
        await redis_module.redis_client.delete(*keys)
    yield
    keys = await redis_module.redis_client.keys("ai:write:*")
    keys += await redis_module.redis_client.keys("ai:quota:*")
    keys += await redis_module.redis_client.keys("ai:failures:*")
    if keys:
        await redis_module.redis_client.delete(*keys)

    # 还原原引用，让后续测试（marketplace 等）的 fixture 重新 reset
    redis_module.redis_pool = original_pool
    redis_module.redis_client = original_client
    exec_mod.redis_client = original_client


class TestL1RateLimit:
    async def test_under_limit_passes(self) -> None:
        """limit 内不抛错"""
        for _ in range(5):
            await check_l1_rate_limit(redis_module.redis_client, 99999, limit=20)
        # 第 5 次后仍在 limit 内，无异常即通过

    async def test_over_limit_raises(self) -> None:
        """超过 limit 抛 AI_RATE_LIMIT_USER_WRITE"""
        user_id = 99998
        for _ in range(3):
            await check_l1_rate_limit(redis_module.redis_client, user_id, limit=3)

        with pytest.raises(BusinessRuleException) as exc_info:
            await check_l1_rate_limit(redis_module.redis_client, user_id, limit=3)
        assert exc_info.value.error_code == "AI_RATE_LIMIT_USER_WRITE"

    async def test_first_call_sets_expire(self) -> None:
        """第一次 INCR 应设 60s EXPIRE"""
        user_id = 99997
        await check_l1_rate_limit(redis_module.redis_client, user_id)
        ttl = await redis_module.redis_client.ttl(f"ai:write:{user_id}")
        # TTL 应该接近 60（刚设置）
        assert 50 <= ttl <= 60


# ============ L2 用户日配额 ============


class TestL2DailyQuota:
    async def test_under_quota_passes(self) -> None:
        for _ in range(10):
            await check_l2_daily_quota(redis_module.redis_client, 99996, limit=100)

    async def test_over_quota_raises(self) -> None:
        user_id = 99995
        for _ in range(2):
            await check_l2_daily_quota(redis_module.redis_client, user_id, limit=2)

        with pytest.raises(BusinessRuleException) as exc_info:
            await check_l2_daily_quota(redis_module.redis_client, user_id, limit=2)
        assert exc_info.value.error_code == "AI_DAILY_QUOTA_EXHAUSTED"


# ============ L3 单 tool 超时 ============


class TestL3Timeout:
    async def test_fast_coro_returns_normally(self) -> None:
        async def fast():
            return "ok"

        result = await with_l3_timeout(fast(), timeout_sec=2)
        assert result == "ok"

    async def test_slow_coro_raises_timeout(self) -> None:
        async def slow():
            await asyncio.sleep(5)

        with pytest.raises(BusinessRuleException) as exc_info:
            await with_l3_timeout(slow(), timeout_sec=1)
        assert exc_info.value.error_code == "AI_TOOL_TIMEOUT"


# ============ 连续失败兜底 ============


class TestRepeatedFailure:
    async def test_first_two_failures_pass(self) -> None:
        """spec §6.5: < 2 次失败不拦截"""
        user_id = 99994
        tool = "user.fail_test"
        args_hash = "hash1"

        await record_failure(
            redis_module.redis_client, user_id, tool, args_hash
        )  # failures=1
        await check_repeated_failure(
            redis_module.redis_client, user_id, tool, args_hash
        )  # 1 < 2，不抛

    async def test_third_failure_blocked(self) -> None:
        """spec §6.5: 失败 >= 2 次后第 3 次抛 AI_REPEATED_FAILURE"""
        user_id = 99993
        tool = "user.fail_test_3"
        args_hash = "hash3"

        await record_failure(redis_module.redis_client, user_id, tool, args_hash)  # 1
        await record_failure(redis_module.redis_client, user_id, tool, args_hash)  # 2

        with pytest.raises(BusinessRuleException) as exc_info:
            await check_repeated_failure(
                redis_module.redis_client, user_id, tool, args_hash
            )
        assert exc_info.value.error_code == "AI_REPEATED_FAILURE"

    async def test_clear_failures_resets(self) -> None:
        """spec §6.5: 成功路径清零失败计数"""
        user_id = 99992
        tool = "user.fail_test_clear"
        args_hash = "hash_clear"

        await record_failure(redis_module.redis_client, user_id, tool, args_hash)
        await clear_failures(redis_module.redis_client, user_id, tool, args_hash)
        # 清零后再检查不抛
        await check_repeated_failure(
            redis_module.redis_client, user_id, tool, args_hash
        )

    async def test_different_args_hash_independent(self) -> None:
        """spec §6.5: 不同 args_hash 失败计数独立"""
        user_id = 99991
        tool = "user.fail_test_diff"

        await record_failure(redis_module.redis_client, user_id, tool, "hash_a")
        await record_failure(redis_module.redis_client, user_id, tool, "hash_a")
        # hash_a 已 2 次，hash_b 仍 0
        await check_repeated_failure(
            redis_module.redis_client, user_id, tool, "hash_b"
        )  # 不抛

    async def test_different_user_independent(self) -> None:
        """spec §6.5: 不同用户的失败计数独立"""
        tool = "user.fail_test_user"
        args_hash = "hash_user"

        await record_failure(redis_module.redis_client, 1, tool, args_hash)
        await record_failure(redis_module.redis_client, 1, tool, args_hash)
        # user 1 已 2 次，user 2 仍 0
        await check_repeated_failure(redis_module.redis_client, 2, tool, args_hash)


# ============ compute_args_hash ============


class TestComputeArgsHash:
    def test_dict_order_independent(self) -> None:
        """sort_keys=True 保证 {a:1,b:2} 与 {b:2,a:1} 同 hash"""
        h1 = compute_args_hash({"a": 1, "b": 2})
        h2 = compute_args_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_different_args_different_hash(self) -> None:
        h1 = compute_args_hash({"user_id": 1})
        h2 = compute_args_hash({"user_id": 2})
        assert h1 != h2

    def test_returns_sha256_hex(self) -> None:
        h = compute_args_hash({"x": 1})
        # SHA256 hex = 64 字符
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_handles_non_serializable(self) -> None:
        """default=str 兼容 datetime / set 等不可序列化对象"""
        from datetime import datetime

        h = compute_args_hash({"ts": datetime(2026, 7, 4)})
        assert len(h) == 64
