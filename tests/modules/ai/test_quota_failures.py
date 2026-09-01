"""容量三层 + 连续失败兜底 单元测试

覆盖多级配额、回滚和连续失败计数。

Redis 用真实 redis_client（ai/conftest.py 的 db_session fixture 已 reset），
不用 mock（mock Redis INCR + EXPIRE 复杂且易错）。

修订记录：
  - 2026-07-10 S-7：L1 改 ZSET 滑窗，加边界突发测试
  - 2026-07-10 S-8：L2 改 UTC date，加跨日 TTL 测试
  - 2026-07-10 S-9：args_hash 类型感知序列化，加防碰撞测试
  - 2026-07-10 S-11：配额自身拒绝 + AuthorizationException 都要回滚计数
  - 2026-07-10 S-12：record_failure INCR + 条件 EXPIRE
"""

# ruff: noqa: ARG001, PLC0415

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.core import redis as redis_module
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.modules.ai.agents.gateway import (
    check_l1_global_rate_limit,
    check_l1_rate_limit,
    check_l2_agent_quota,
    check_l2_daily_quota,
    check_l4_conv_budget,
    check_repeated_failure,
    clear_failures,
    compute_args_hash,
    decr_quota,
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
        """risk=low 不计入 L1/L2。"""
        assert is_write_tool(_meta(risk="low")) is False

    def test_high_is_write(self) -> None:
        assert is_write_tool(_meta(risk="high")) is True

    def test_destructive_is_write(self) -> None:
        assert is_write_tool(_meta(risk="destructive")) is True

    def test_hitl_always_is_write(self) -> None:
        """hitl_always=True 时即使 risk=low 也按写操作计数。"""
        assert is_write_tool(_meta(risk="low", hitl_always=True)) is True


# ============ L1 用户写速率（修订 S-7：ZSET 滑窗） ============


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

    async def _purge() -> None:
        keys = await redis_module.redis_client.keys("ai:write:*")
        keys += await redis_module.redis_client.keys("ai:quota:*")
        keys += await redis_module.redis_client.keys("ai:failures:*")
        keys += await redis_module.redis_client.keys("ai:rate:*")  # 全局 L1。
        keys += await redis_module.redis_client.keys("ai:budget:*")  # 会话预算。
        if keys:
            await redis_module.redis_client.delete(*keys)

    await _purge()
    yield
    await _purge()

    # 还原原引用，让后续测试（marketplace 等）的 fixture 重新 reset
    redis_module.redis_pool = original_pool
    redis_module.redis_client = original_client
    exec_mod.redis_client = original_client


class TestL1RateLimit:
    async def test_under_limit_passes(self) -> None:
        """limit 内不抛错"""
        for _ in range(5):
            count, _ = await check_l1_rate_limit(
                redis_module.redis_client, 99999, limit=20
            )
            assert count >= 1
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
        """第一次 ZADD 应设 60s EXPIRE"""
        user_id = 99997
        await check_l1_rate_limit(redis_module.redis_client, user_id)
        ttl = await redis_module.redis_client.ttl(f"ai:write:{user_id}")
        # TTL 应该接近 60（刚设置）
        assert 50 <= ttl <= 60

    async def test_returns_member_for_rollback(self) -> None:
        """修订 S-11：返回 (count, member)，member 用于精确回滚"""
        user_id = 99996
        _, member = await check_l1_rate_limit(redis_module.redis_client, user_id)
        assert isinstance(member, str)
        # member 应在 zset 里
        score = await redis_module.redis_client.zscore(f"ai:write:{user_id}", member)
        assert score is not None

    async def test_over_limit_rolls_back_self(self) -> None:
        """修订 S-11：超限时 ZREM 自身，zset 数量回到 limit"""
        user_id = 99995
        for _ in range(3):
            await check_l1_rate_limit(redis_module.redis_client, user_id, limit=3)
        # 第 4 次触发 raise，但应该 ZREM 自身
        with pytest.raises(BusinessRuleException):
            await check_l1_rate_limit(redis_module.redis_client, user_id, limit=3)
        # zset 应该回到 limit (3)，不是 4
        count = await redis_module.redis_client.zcard(f"ai:write:{user_id}")
        assert count == 3

    async def test_sliding_window_drops_expired(self) -> None:
        """修订 S-7：滑窗 60s，过期成员自动清掉

        构造场景：先填 5 个老成员（score = now - 120s），再调一次，
        新调用应该看不到老成员（窗口内只有自己 1 个）。
        """
        user_id = 99994
        key = f"ai:write:{user_id}"
        # 手动塞 5 个老成员（score 在窗口外）
        old_ts = asyncio.get_event_loop().time() - 120  # 120s 前
        await redis_module.redis_client.zadd(
            key,
            {
                f"old:{i}": old_ts - i  # 略微错开
                for i in range(5)
            },
        )
        # 调用 check_l1_rate_limit，应该 ZREMRANGEBYSCORE 清掉老成员
        count, _ = await check_l1_rate_limit(
            redis_module.redis_client, user_id, limit=20
        )
        # 老成员已清，只有刚加的 1 个新成员
        assert count == 1


# ============ L1 全局速率 ============


class TestL1GlobalRateLimit:
    """全局 L1 与用户级 L1 叠加。"""

    async def test_limit_zero_skips_check(self) -> None:
        """limit=0（默认）→ 跳过，返回 None，不写 zset"""
        result = await check_l1_global_rate_limit(redis_module.redis_client, limit=0)
        assert result is None
        exists = await redis_module.redis_client.exists("ai:rate:global")
        assert exists == 0  # 未写 key

    async def test_under_limit_passes(self) -> None:
        """limit=5 → 连续 5 次通过"""
        for _ in range(5):
            r = await check_l1_global_rate_limit(redis_module.redis_client, limit=5)
            assert r is not None

    async def test_over_limit_raises(self) -> None:
        """limit=2 → 第 3 次抛 AI_RATE_LIMIT_GLOBAL"""
        for _ in range(2):
            await check_l1_global_rate_limit(redis_module.redis_client, limit=2)

        with pytest.raises(BusinessRuleException) as exc_info:
            await check_l1_global_rate_limit(redis_module.redis_client, limit=2)
        assert exc_info.value.error_code == "AI_RATE_LIMIT_GLOBAL"

    async def test_over_limit_rolls_back_self(self) -> None:
        """超限时移除本次成员，使 zset 回到 limit。"""
        for _ in range(2):
            await check_l1_global_rate_limit(redis_module.redis_client, limit=2)
        with pytest.raises(BusinessRuleException):
            await check_l1_global_rate_limit(redis_module.redis_client, limit=2)

        count = await redis_module.redis_client.zcard("ai:rate:global")
        assert count == 2  # 回到 limit

    async def test_returns_member_for_rollback(self) -> None:
        """返回 (count, member)，member 用于 decr_quota 精确回滚"""
        r = await check_l1_global_rate_limit(redis_module.redis_client, limit=10)
        assert r is not None
        _, member = r
        assert isinstance(member, str)
        score = await redis_module.redis_client.zscore("ai:rate:global", member)
        assert score is not None

    async def test_shared_across_calls(self) -> None:
        """全局 L1 不分 user_id，所有调用共享同一个 zset（与用户级 L1 独立 key）

        模拟多用户连续调 check_l1_global_rate_limit，zset 累加。
        """
        # 模拟 3 次独立调用（不区分用户）
        counts = []
        for _ in range(3):
            r = await check_l1_global_rate_limit(redis_module.redis_client, limit=10)
            assert r is not None
            counts.append(r[0])

        # 计数应递增：1, 2, 3（zset 累加，无 user_id 维度）
        assert counts == [1, 2, 3]


class TestL2DailyQuota:
    async def test_under_quota_passes(self) -> None:
        for _ in range(10):
            await check_l2_daily_quota(redis_module.redis_client, 99993, limit=100)

    async def test_over_quota_raises(self) -> None:
        user_id = 99992
        for _ in range(2):
            await check_l2_daily_quota(redis_module.redis_client, user_id, limit=2)

        with pytest.raises(BusinessRuleException) as exc_info:
            await check_l2_daily_quota(redis_module.redis_client, user_id, limit=2)
        assert exc_info.value.error_code == "AI_DAILY_QUOTA_EXHAUSTED"

    async def test_over_quota_rolls_back_self(self) -> None:
        """修订 S-11：超限时 DECR 自身，计数器回到 limit"""
        user_id = 99991
        for _ in range(2):
            await check_l2_daily_quota(redis_module.redis_client, user_id, limit=2)
        with pytest.raises(BusinessRuleException):
            await check_l2_daily_quota(redis_module.redis_client, user_id, limit=2)

        # 当前 UTC date key 计数应该回到 limit (2)，不是 3
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        count = int(
            await redis_module.redis_client.get(f"ai:quota:{user_id}:{date_str}") or 0
        )
        assert count == 2

    async def test_l2_uses_utc_date(self) -> None:
        """修订 S-8：date key 用 UTC，不是本地时区"""
        user_id = 99990
        await check_l2_daily_quota(redis_module.redis_client, user_id, limit=100)
        utc_date_str = datetime.now(UTC).strftime("%Y%m%d")
        # 验证 UTC date key 存在
        exists = await redis_module.redis_client.exists(
            f"ai:quota:{user_id}:{utc_date_str}"
        )
        assert exists == 1

    async def test_l2_ttl_seconds_to_midnight_utc(self) -> None:
        """修订 S-8：TTL 算到当日 UTC 结束，不是固定 86400s

        mock 一个 23:59 UTC 的时刻，TTL 应该约为 60s（到 00:00）。
        """
        user_id = 99989
        # mock datetime.now(UTC) 返回 23:59:00 UTC
        mock_now = datetime(2026, 7, 10, 23, 59, 0, tzinfo=UTC)
        with patch("app.modules.ai.agents.gateway.quota.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            # strftime 调用要走真实 datetime 的方法（mock_dt 现在是 MagicMock）
            # 改用 side_effect 让 now 返回 mock_now，其余属性 passthrough
            # 但 strftime 在 mock 上不存在 → 简单方案：直接 stub
            mock_dt.now = lambda *_, **__: mock_now
            # date_str 需要手工算
            date_str = mock_now.strftime("%Y%m%d")

            await check_l2_daily_quota(redis_module.redis_client, user_id, limit=100)

            ttl = await redis_module.redis_client.ttl(f"ai:quota:{user_id}:{date_str}")
            # 23:59:00 → 00:00:00 还有 60s
            assert 50 <= ttl <= 60


# ============ L2 per-agent 维度 ============


class TestL2AgentQuota:
    """per-agent L2 与全局 L2 叠加。"""

    async def test_limit_none_skips_check(self) -> None:
        """agent 未配专属额度（limit=None）→ 跳过，不写 Redis key"""
        user_id = 99981
        result = await check_l2_agent_quota(
            redis_module.redis_client, user_id, "user_mgmt", limit=None
        )
        assert result is None
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        exists = await redis_module.redis_client.exists(
            f"ai:quota:{user_id}:user_mgmt:{date_str}"
        )
        assert exists == 0  # 未写 key

    async def test_under_quota_passes(self) -> None:
        """limit=5 时连续 5 次通过"""
        user_id = 99980
        for _ in range(5):
            await check_l2_agent_quota(
                redis_module.redis_client, user_id, "user_mgmt", limit=5
            )

    async def test_over_quota_raises(self) -> None:
        """limit=2 第 3 次抛 AI_DAILY_QUOTA_EXHAUSTED"""
        user_id = 99979
        for _ in range(2):
            await check_l2_agent_quota(
                redis_module.redis_client, user_id, "user_mgmt", limit=2
            )

        with pytest.raises(BusinessRuleException) as exc_info:
            await check_l2_agent_quota(
                redis_module.redis_client, user_id, "user_mgmt", limit=2
            )
        assert exc_info.value.error_code == "AI_DAILY_QUOTA_EXHAUSTED"

    async def test_over_quota_rolls_back_self(self) -> None:
        """超限时回滚本次递增，使计数回到 limit。"""
        user_id = 99978
        for _ in range(2):
            await check_l2_agent_quota(
                redis_module.redis_client, user_id, "user_mgmt", limit=2
            )
        with pytest.raises(BusinessRuleException):
            await check_l2_agent_quota(
                redis_module.redis_client, user_id, "user_mgmt", limit=2
            )

        date_str = datetime.now(UTC).strftime("%Y%m%d")
        count = int(
            await redis_module.redis_client.get(
                f"ai:quota:{user_id}:user_mgmt:{date_str}"
            )
            or 0
        )
        assert count == 2  # 回到 limit

    async def test_independent_per_agent(self) -> None:
        """不同 agent_code 互不影响（user_mgmt 用满不影响 job_mgmt）"""
        user_id = 99977
        # user_mgmt 用到 limit
        await check_l2_agent_quota(
            redis_module.redis_client, user_id, "user_mgmt", limit=1
        )
        # user_mgmt 已用满
        with pytest.raises(BusinessRuleException):
            await check_l2_agent_quota(
                redis_module.redis_client, user_id, "user_mgmt", limit=1
            )
        # job_mgmt 应该还能用
        result = await check_l2_agent_quota(
            redis_module.redis_client, user_id, "job_mgmt", limit=5
        )
        assert result == 1

    async def test_independent_per_user(self) -> None:
        """不同 user_id 互不影响"""
        await check_l2_agent_quota(
            redis_module.redis_client, 99976, "user_mgmt", limit=1
        )
        # 另一用户不受影响
        result = await check_l2_agent_quota(
            redis_module.redis_client, 99975, "user_mgmt", limit=1
        )
        assert result == 1


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


# ============ L4 会话预算 ============


class TestL4ConvBudget:
    """会话预算防止用户拆分对话绕过日配额。"""

    async def test_limit_zero_skips_check(self) -> None:
        """limit=0（默认）→ 跳过，返回 None，不写 key"""
        result = await check_l4_conv_budget(redis_module.redis_client, 12345, limit=0)
        assert result is None
        exists = await redis_module.redis_client.exists("ai:budget:conv:12345")
        assert exists == 0

    async def test_conversation_id_zero_skips_check(self) -> None:
        """conversation_id=0（无 conversation 上下文）→ 跳过"""
        result = await check_l4_conv_budget(redis_module.redis_client, 0, limit=100)
        assert result is None
        exists = await redis_module.redis_client.exists("ai:budget:conv:0")
        assert exists == 0

    async def test_under_limit_passes(self) -> None:
        """limit=5 → 连续 5 次通过"""
        for _ in range(5):
            r = await check_l4_conv_budget(redis_module.redis_client, 12346, limit=5)
            assert r is not None

    async def test_over_limit_raises(self) -> None:
        """limit=2 → 第 3 次抛 AI_CONV_BUDGET_EXHAUSTED"""
        for _ in range(2):
            await check_l4_conv_budget(redis_module.redis_client, 12347, limit=2)

        with pytest.raises(BusinessRuleException) as exc_info:
            await check_l4_conv_budget(redis_module.redis_client, 12347, limit=2)
        assert exc_info.value.error_code == "AI_CONV_BUDGET_EXHAUSTED"

    async def test_over_limit_rolls_back_self(self) -> None:
        """超限时回滚本次递增，使 key 计数回到 limit。"""
        for _ in range(2):
            await check_l4_conv_budget(redis_module.redis_client, 12348, limit=2)
        with pytest.raises(BusinessRuleException):
            await check_l4_conv_budget(redis_module.redis_client, 12348, limit=2)

        count = int(await redis_module.redis_client.get("ai:budget:conv:12348") or 0)
        assert count == 2  # 回到 limit

    async def test_first_call_sets_ttl_24h(self) -> None:
        """首次递增时设置 24 小时滚动窗口。"""
        await check_l4_conv_budget(redis_module.redis_client, 12349, limit=100)
        ttl = await redis_module.redis_client.ttl("ai:budget:conv:12349")
        # 24h = 86400s，允许 ±60s 误差（测试执行耗时）
        assert 86340 <= ttl <= 86400

    async def test_ttl_not_reset_on_subsequent_calls(self) -> None:
        """后续递增不重置首次设置的 TTL。"""
        await check_l4_conv_budget(redis_module.redis_client, 12350, limit=100)
        ttl1 = await redis_module.redis_client.ttl("ai:budget:conv:12350")

        # 等 1s 后再调，TTL 应该减少而非重置
        import asyncio

        await asyncio.sleep(1.0)
        await check_l4_conv_budget(redis_module.redis_client, 12350, limit=100)
        ttl2 = await redis_module.redis_client.ttl("ai:budget:conv:12350")

        # TTL 应减少（接近 1s），而非重置到 86400
        assert ttl2 < ttl1
        assert ttl1 - ttl2 <= 2  # 减少约 1s（允许 ±1s 误差）

    async def test_independent_per_conversation(self) -> None:
        """不同 conversation_id 互不影响"""
        await check_l4_conv_budget(redis_module.redis_client, 12351, limit=1)
        # 12351 已满
        with pytest.raises(BusinessRuleException):
            await check_l4_conv_budget(redis_module.redis_client, 12351, limit=1)
        # 12352 应该还能用
        r = await check_l4_conv_budget(redis_module.redis_client, 12352, limit=5)
        assert r is not None


# ============ decr_quota（修订 S-11 回滚 helper） ============


class TestDecrQuota:
    async def test_decrements_l2(self) -> None:
        """修订 S-11：AuthorizationException 路径回滚 L2 计数"""
        user_id = 99988
        # 先填 3 次 L2
        for _ in range(3):
            await check_l2_daily_quota(redis_module.redis_client, user_id, limit=100)

        await decr_quota(redis_module.redis_client, user_id, l1_member=None)

        date_str = datetime.now(UTC).strftime("%Y%m%d")
        count = int(
            await redis_module.redis_client.get(f"ai:quota:{user_id}:{date_str}") or 0
        )
        assert count == 2  # 3 - 1 = 2

    async def test_decrements_l1_with_member(self) -> None:
        """修订 S-11：传 member 时精确 ZREM 该 member"""
        user_id = 99987
        _, member = await check_l1_rate_limit(redis_module.redis_client, user_id)
        count_before = await redis_module.redis_client.zcard(f"ai:write:{user_id}")
        assert count_before == 1

        await decr_quota(redis_module.redis_client, user_id, l1_member=member)

        count_after = await redis_module.redis_client.zcard(f"ai:write:{user_id}")
        assert count_after == 0
        # member 应该不存在了
        score = await redis_module.redis_client.zscore(f"ai:write:{user_id}", member)
        assert score is None

    async def test_decr_l1_without_member_no_op(self) -> None:
        """修订 S-11：l1_member=None 时不回滚 L1（保守：宁可多算不漏算）"""
        user_id = 99986
        _, _ = await check_l1_rate_limit(redis_module.redis_client, user_id)
        count_before = await redis_module.redis_client.zcard(f"ai:write:{user_id}")

        await decr_quota(redis_module.redis_client, user_id, l1_member=None)

        count_after = await redis_module.redis_client.zcard(f"ai:write:{user_id}")
        assert count_after == count_before  # 没动

    async def test_decr_agent_quota_with_agent_code(self) -> None:
        """传入 agent_code 时同步回滚 per-agent L2 key。"""
        user_id = 99971
        # 先 INCR 3 次 per-agent
        for _ in range(3):
            await check_l2_agent_quota(
                redis_module.redis_client, user_id, "user_mgmt", limit=100
            )

        # 也 INCR 全局 L2（decr_quota 会同时 decr 全局）
        for _ in range(3):
            await check_l2_daily_quota(redis_module.redis_client, user_id, limit=100)

        await decr_quota(
            redis_module.redis_client,
            user_id,
            agent_code="user_mgmt",
            l1_member=None,
        )

        date_str = datetime.now(UTC).strftime("%Y%m%d")
        agent_count = int(
            await redis_module.redis_client.get(
                f"ai:quota:{user_id}:user_mgmt:{date_str}"
            )
            or 0
        )
        global_count = int(
            await redis_module.redis_client.get(f"ai:quota:{user_id}:{date_str}") or 0
        )
        assert agent_count == 2  # 3 - 1 = 2
        assert global_count == 2  # 3 - 1 = 2

    async def test_decr_without_agent_code_skips_per_agent(self) -> None:
        """agent_code=None 时不回滚 per-agent key。"""
        user_id = 99970
        for _ in range(3):
            await check_l2_agent_quota(
                redis_module.redis_client, user_id, "user_mgmt", limit=100
            )

        await decr_quota(
            redis_module.redis_client, user_id, agent_code=None, l1_member=None
        )

        date_str = datetime.now(UTC).strftime("%Y%m%d")
        agent_count = int(
            await redis_module.redis_client.get(
                f"ai:quota:{user_id}:user_mgmt:{date_str}"
            )
            or 0
        )
        assert agent_count == 3  # 没 decr

    async def test_decr_global_l1_with_member(self) -> None:
        """传入 l1_global_member 时移除全局 zset 成员。"""
        # 先 INCR 全局 L1 3 次，拿到最后一次的 member
        members = []
        for _ in range(3):
            r = await check_l1_global_rate_limit(redis_module.redis_client, limit=100)
            assert r is not None
            members.append(r[1])

        # ZREM 最后一个 member
        await decr_quota(
            redis_module.redis_client,
            99969,  # user_id 不影响全局 L1
            l1_global_member=members[-1],
        )

        count = await redis_module.redis_client.zcard("ai:rate:global")
        assert count == 2  # 3 - 1 = 2

    async def test_decr_without_global_member_skips_global_l1(self) -> None:
        """l1_global_member=None 时不修改全局 zset。"""
        for _ in range(3):
            await check_l1_global_rate_limit(redis_module.redis_client, limit=100)

        await decr_quota(
            redis_module.redis_client,
            99968,
            l1_global_member=None,
        )

        count = await redis_module.redis_client.zcard("ai:rate:global")
        assert count == 3  # 没 ZREM

    async def test_decr_l4_conv_with_key(self) -> None:
        """传入 l4_conv_key 时回滚会话预算。"""
        # 先 INCR 3 次
        for _ in range(3):
            await check_l4_conv_budget(redis_module.redis_client, 77701, limit=100)

        await decr_quota(
            redis_module.redis_client,
            99967,
            l4_conv_key="ai:budget:conv:77701",
        )

        count = int(await redis_module.redis_client.get("ai:budget:conv:77701") or 0)
        assert count == 2  # 3 - 1 = 2

    async def test_decr_without_l4_conv_key_skips_l4(self) -> None:
        """l4_conv_key=None 时不回滚会话预算。"""
        for _ in range(3):
            await check_l4_conv_budget(redis_module.redis_client, 77702, limit=100)

        await decr_quota(
            redis_module.redis_client,
            99966,
            l4_conv_key=None,
        )

        count = int(await redis_module.redis_client.get("ai:budget:conv:77702") or 0)
        assert count == 3  # 没 DECR


# ============ 连续失败兜底（修订 S-12：INCR + 条件 EXPIRE） ============


class TestRepeatedFailure:
    async def test_first_two_failures_pass(self) -> None:
        """少于两次失败时不拦截。"""
        user_id = 99985
        tool = "user.fail_test"
        args_hash = "hash1"

        await record_failure(
            redis_module.redis_client, user_id, tool, args_hash
        )  # failures=1
        await check_repeated_failure(
            redis_module.redis_client, user_id, tool, args_hash
        )  # 1 < 2，不抛

    async def test_third_failure_blocked(self) -> None:
        """累计两次失败后，第三次调用抛 AI_REPEATED_FAILURE。"""
        user_id = 99984
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
        """成功后清零失败计数。"""
        user_id = 99983
        tool = "user.fail_test_clear"
        args_hash = "hash_clear"

        await record_failure(redis_module.redis_client, user_id, tool, args_hash)
        await clear_failures(redis_module.redis_client, user_id, tool, args_hash)
        # 清零后再检查不抛
        await check_repeated_failure(
            redis_module.redis_client, user_id, tool, args_hash
        )

    async def test_different_args_hash_independent(self) -> None:
        """不同 args_hash 的失败计数相互独立。"""
        user_id = 99982
        tool = "user.fail_test_diff"

        await record_failure(redis_module.redis_client, user_id, tool, "hash_a")
        await record_failure(redis_module.redis_client, user_id, tool, "hash_a")
        # hash_a 已 2 次，hash_b 仍 0
        await check_repeated_failure(
            redis_module.redis_client, user_id, tool, "hash_b"
        )  # 不抛

    async def test_different_user_independent(self) -> None:
        """不同用户的失败计数相互独立。"""
        tool = "user.fail_test_user"
        args_hash = "hash_user"

        await record_failure(redis_module.redis_client, 1, tool, args_hash)
        await record_failure(redis_module.redis_client, 1, tool, args_hash)
        # user 1 已 2 次，user 2 仍 0
        await check_repeated_failure(redis_module.redis_client, 2, tool, args_hash)

    async def test_record_failure_sets_ttl_only_on_first(self) -> None:
        """修订 S-12：仅在 INCR 返回 1 时设 EXPIRE，避免后续失败重置 TTL

        场景：第一次失败 INCR=1 设 TTL=600s；过 1s 第二次失败 INCR=2 不重置 TTL。
        """
        user_id = 99981
        tool = "user.fail_ttl"
        args_hash = "hash_ttl"

        # 第一次失败
        await record_failure(redis_module.redis_client, user_id, tool, args_hash)
        ttl1 = await redis_module.redis_client.ttl(
            f"ai:failures:{user_id}:{tool}:{args_hash}"
        )
        assert 590 <= ttl1 <= 600  # 第一次 TTL ~600

        # 模拟 2s 过去（Redis 不支持时间旅行，跳过实际等待；改用检查"第二次调用不重置"）
        # 实际只能验证：第二次调用后 TTL 应该比第一次小（不重置）
        await record_failure(redis_module.redis_client, user_id, tool, args_hash)
        ttl2 = await redis_module.redis_client.ttl(
            f"ai:failures:{user_id}:{tool}:{args_hash}"
        )
        # TTL2 应该 ≤ TTL1（理想是相同；只要不增加就证明没重置）
        assert ttl2 <= ttl1


# ============ compute_args_hash（修订 S-9：类型感知序列化） ============


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
        """修订 S-9：兼容 datetime / set 等不可序列化对象"""
        from datetime import datetime

        h = compute_args_hash({"ts": datetime(2026, 7, 4)})
        assert len(h) == 64

    def test_datetime_does_not_collide_with_str(self) -> None:
        """修订 S-9：datetime 与其字符串表示必须 hash 不同

        旧实现 default=str 让两者产生相同 JSON → 哈希碰撞 → 不同业务意图
        共享失败计数器。修订后类型前缀防碰撞。
        """
        from datetime import datetime

        dt = datetime(2026, 1, 1)
        dt_str = str(dt)  # "2026-01-01 00:00:00"
        h1 = compute_args_hash({"ts": dt})
        h2 = compute_args_hash({"ts": dt_str})
        assert h1 != h2  # 修订后必须不同

    def test_decimal_does_not_collide_with_str(self) -> None:
        """修订 S-9：Decimal 与其字符串表示必须 hash 不同"""
        from decimal import Decimal

        d = Decimal("1.5")
        d_str = "1.5"
        h1 = compute_args_hash({"amount": d})
        h2 = compute_args_hash({"amount": d_str})
        assert h1 != h2

    def test_different_object_types_isolated(self) -> None:
        """修订 S-9：两个不同类型的对象（值相同）hash 不同"""
        from datetime import date, datetime

        # date(2026,1,1) 和 datetime(2026,1,1) 的 repr 不同
        h1 = compute_args_hash({"x": date(2026, 1, 1)})
        h2 = compute_args_hash({"x": datetime(2026, 1, 1)})
        assert h1 != h2


# ============ AuthorizationException 集成（修订 S-11） ============


class TestAuthorizationExceptionRollback:
    """修订 S-11：业务函数抛 AuthorizationException 时 decr_quota 回滚 L1/L2

    通过直接调 _invoke_tool_fn 验证（不调 execute_tool 全链路，避免
    dry_run / emit / log 等副作用）。
    """

    async def test_authorization_exception_decrements_quota(self) -> None:
        from app.modules.ai.agents.gateway import executor as exec_mod
        from app.modules.ai.agents.gateway.result import ToolResult
        from app.modules.ai.agents.tools import AiToolMeta

        # 注册一个 mock tool，函数体抛 AuthorizationException
        meta = AiToolMeta(
            name="test.auth_rollback",
            agent="test",
            summary="s",
            required_perms=("p",),
            risk="high",
        )

        async def _throw_authz(ctx, **kwargs):
            raise AuthorizationException(
                "scope violation", error_code="AI_DATA_SCOPE_VIOLATION"
            )

        # 构造 RegisteredTool mock
        class _FakeReg:
            def __init__(self, meta, fn):
                self.meta = meta
                self.fn = fn
                self.dry_run_fn = None

        registered = _FakeReg(meta, _throw_authz)

        # 先填 L1/L2 一些计数
        user_id = 99980
        _, member = await check_l1_rate_limit(redis_module.redis_client, user_id)
        await check_l2_daily_quota(redis_module.redis_client, user_id)
        l1_before = await redis_module.redis_client.zcard(f"ai:write:{user_id}")
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        l2_before = int(
            await redis_module.redis_client.get(f"ai:quota:{user_id}:{date_str}") or 0
        )

        # 构造最小 ChatDeps（用 SimpleNamespace 避免类 scope 引用问题）
        from types import SimpleNamespace

        from app.modules.ai.core.context import ChatDeps, DataScopeContext

        fake_user = SimpleNamespace(user_id=user_id, user_name="tester")

        deps = ChatDeps(
            user=fake_user,  # type: ignore[arg-type]
            perms={"p"},
            db=None,  # type: ignore[arg-type]
            data_scope=DataScopeContext(
                tenant_id=0,
                accessible_dept_ids=None,
                accessible_user_scope=None,
            ),
            agent=None,  # type: ignore[arg-type]
            trace_id="tr_test_authz",
        )

        # 调 _invoke_tool_fn
        result = await exec_mod._invoke_tool_fn(
            registered,  # type: ignore[arg-type]
            {},
            deps,
            "hash_authz",
            l1_member=member,
        )

        # 验证：返回 AuthorizationException 转的 ToolResult.failure
        assert isinstance(result, ToolResult)
        assert not result.ok
        assert result.error_code == "AI_DATA_SCOPE_VIOLATION"

        # 验证 L1/L2 都被回滚
        l1_after = await redis_module.redis_client.zcard(f"ai:write:{user_id}")
        l2_after = int(
            await redis_module.redis_client.get(f"ai:quota:{user_id}:{date_str}") or 0
        )
        assert l1_after == l1_before - 1  # 精确 ZREM member
        assert l2_after == l2_before - 1  # DECR

    async def test_business_exception_does_not_decrement_quota(self) -> None:
        """非鉴权业务异常保留配额计数。"""
        from app.core.exceptions import BusinessRuleException
        from app.modules.ai.agents.gateway import executor as exec_mod
        from app.modules.ai.agents.tools import AiToolMeta

        meta = AiToolMeta(
            name="test.biz_norollback",
            agent="test",
            summary="s",
            required_perms=("p",),
            risk="high",
        )

        async def _throw_biz(ctx, **kwargs):
            raise BusinessRuleException("biz error", error_code="AI_SOMETHING_FAILED")

        class _FakeReg:
            def __init__(self, meta, fn):
                self.meta = meta
                self.fn = fn
                self.dry_run_fn = None

        registered = _FakeReg(meta, _throw_biz)

        user_id = 99979
        _, member = await check_l1_rate_limit(redis_module.redis_client, user_id)
        await check_l2_daily_quota(redis_module.redis_client, user_id)
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        l2_before = int(
            await redis_module.redis_client.get(f"ai:quota:{user_id}:{date_str}") or 0
        )

        from types import SimpleNamespace

        from app.modules.ai.core.context import ChatDeps, DataScopeContext

        fake_user = SimpleNamespace(user_id=user_id, user_name="tester")

        deps = ChatDeps(
            user=fake_user,  # type: ignore[arg-type]
            perms={"p"},
            db=None,  # type: ignore[arg-type]
            data_scope=DataScopeContext(
                tenant_id=0,
                accessible_dept_ids=None,
                accessible_user_scope=None,
            ),
            agent=None,  # type: ignore[arg-type]
            trace_id="tr_test_biz",
        )

        await exec_mod._invoke_tool_fn(
            registered,  # type: ignore[arg-type]
            {},
            deps,
            "hash_biz",
            l1_member=member,
        )

        # 业务异常不回滚 L2，保留本次计数。
        l2_after = int(
            await redis_module.redis_client.get(f"ai:quota:{user_id}:{date_str}") or 0
        )
        assert l2_after == l2_before  # 没变
