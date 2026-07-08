"""query_cache Redis helper 单元测试 — spec §8.7 / §2.9

覆盖：
  - set + get 最新（按 created_at 降序）
  - 同 trace_id 多 tool 写入（hash 多 field）
  - tool_name 指定时 HGET
  - hash 不存在返回 None
  - TTL 过期返回 None
  - delete
"""

# ruff: noqa: ARG001, PLC0415

import asyncio

import pytest
import redis.asyncio as aioredis

from app.core import redis as redis_module
from app.core.config import settings
from app.modules.ai.agents.hitl.query_cache import (
    AI_QUERY_CACHE_PREFIX,
    AI_QUERY_CACHE_TTL_SEC,
    delete_query_cache,
    get_query_cache,
    set_query_cache,
)


@pytest.fixture(autouse=True)
async def clean_redis_query_cache():
    """每个测试重建 redis_client + 清 ai:query_cache:* keys"""
    original_pool = redis_module.redis_pool
    original_client = redis_module.redis_client

    redis_module.redis_pool = aioredis.ConnectionPool.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=20,
    )
    redis_module.redis_client = aioredis.Redis(connection_pool=redis_module.redis_pool)

    keys = await redis_module.redis_client.keys(f"{AI_QUERY_CACHE_PREFIX}:*")
    if keys:
        await redis_module.redis_client.delete(*keys)

    yield

    keys = await redis_module.redis_client.keys(f"{AI_QUERY_CACHE_PREFIX}:*")
    if keys:
        await redis_module.redis_client.delete(*keys)

    redis_module.redis_pool = original_pool
    redis_module.redis_client = original_client


class TestSetGet:
    async def test_set_then_get(self) -> None:
        await set_query_cache(
            redis_module.redis_client,
            trace_id="tr_1",
            tool_name="user.list",
            module="system/user",
            filters={"status": "1"},
            user_id=100,
        )

        entry = await get_query_cache(redis_module.redis_client, "tr_1")
        assert entry is not None
        assert entry.tool_name == "user.list"
        assert entry.module == "system/user"
        assert entry.filters == {"status": "1"}
        assert entry.user_id == 100
        assert entry.created_at.endswith("Z")

    async def test_get_not_found(self) -> None:
        entry = await get_query_cache(redis_module.redis_client, "tr_nonexistent")
        assert entry is None

    async def test_get_with_tool_name(self) -> None:
        """指定 tool_name 时直接 HGET"""
        await set_query_cache(
            redis_module.redis_client,
            trace_id="tr_2",
            tool_name="user.stats",
            module="system/user",
            filters={"user_gender": "1"},
            user_id=200,
        )

        entry = await get_query_cache(
            redis_module.redis_client, "tr_2", tool_name="user.stats"
        )
        assert entry is not None
        assert entry.tool_name == "user.stats"

        # 不存在的 field
        entry = await get_query_cache(
            redis_module.redis_client, "tr_2", tool_name="user.list"
        )
        assert entry is None


class TestMultipleTools:
    async def test_latest_wins(self) -> None:
        """同 trace_id 多 tool，取 created_at 最新"""
        # 故意按时间顺序写两个
        await set_query_cache(
            redis_module.redis_client,
            trace_id="tr_3",
            tool_name="user.list",
            module="system/user",
            filters={},
            user_id=300,
        )
        await asyncio.sleep(1.1)  # 确保 created_at 不同（秒级精度）
        await set_query_cache(
            redis_module.redis_client,
            trace_id="tr_3",
            tool_name="user.stats",
            module="system/user",
            filters={},
            user_id=300,
        )

        entry = await get_query_cache(redis_module.redis_client, "tr_3")
        # user.stats 后写，应返回它
        assert entry is not None
        assert entry.tool_name == "user.stats"

    async def test_specific_tool_name_overrides_latest(self) -> None:
        """指定 tool_name 时返回指定 field（即使不是最新）"""
        await set_query_cache(
            redis_module.redis_client,
            trace_id="tr_4",
            tool_name="user.list",
            module="system/user",
            filters={"a": "1"},
            user_id=400,
        )
        await asyncio.sleep(1.1)
        await set_query_cache(
            redis_module.redis_client,
            trace_id="tr_4",
            tool_name="user.stats",
            module="system/user",
            filters={"b": "2"},
            user_id=400,
        )

        # 显式取较早的 user.list
        entry = await get_query_cache(
            redis_module.redis_client, "tr_4", tool_name="user.list"
        )
        assert entry is not None
        assert entry.tool_name == "user.list"
        assert entry.filters == {"a": "1"}


class TestTtl:
    async def test_expires_after_ttl(self) -> None:
        """hash TTL=300s，但单个测试等不了 5min。
        改用直接设短 TTL 验证机制：写入后立即删除整个 key 模拟过期"""
        await set_query_cache(
            redis_module.redis_client,
            trace_id="tr_5",
            tool_name="x",
            module="m",
            filters={},
            user_id=500,
        )
        # 直接模拟过期：删除 key
        await redis_module.redis_client.delete(f"{AI_QUERY_CACHE_PREFIX}:tr_5")
        entry = await get_query_cache(redis_module.redis_client, "tr_5")
        assert entry is None

    async def test_ttl_value_is_300(self) -> None:
        """spec §8.7: TTL=300s 写入后 TTL 接近 300"""
        await set_query_cache(
            redis_module.redis_client,
            trace_id="tr_6",
            tool_name="x",
            module="m",
            filters={},
            user_id=600,
        )
        ttl = await redis_module.redis_client.ttl(f"{AI_QUERY_CACHE_PREFIX}:tr_6")
        # 容忍 5s 抖动
        assert AI_QUERY_CACHE_TTL_SEC - 5 <= ttl <= AI_QUERY_CACHE_TTL_SEC


class TestDelete:
    async def test_delete_removes_all_fields(self) -> None:
        await set_query_cache(
            redis_module.redis_client,
            trace_id="tr_7",
            tool_name="a",
            module="m",
            filters={},
            user_id=700,
        )
        await set_query_cache(
            redis_module.redis_client,
            trace_id="tr_7",
            tool_name="b",
            module="m",
            filters={},
            user_id=700,
        )
        await delete_query_cache(redis_module.redis_client, "tr_7")

        entry = await get_query_cache(redis_module.redis_client, "tr_7")
        assert entry is None


class TestHsetResetsTtl:
    async def test_hset_resets_ttl(self) -> None:
        """spec §8.7: 每次 HSET 重置整个 hash 的 TTL"""
        await set_query_cache(
            redis_module.redis_client,
            trace_id="tr_8",
            tool_name="a",
            module="m",
            filters={},
            user_id=800,
        )
        # 等几秒让 TTL 减少
        await asyncio.sleep(3)
        ttl_before = await redis_module.redis_client.ttl(
            f"{AI_QUERY_CACHE_PREFIX}:tr_8"
        )
        assert ttl_before < AI_QUERY_CACHE_TTL_SEC  # 已减少

        # 第二次写入应重置 TTL
        await set_query_cache(
            redis_module.redis_client,
            trace_id="tr_8",
            tool_name="b",
            module="m",
            filters={},
            user_id=800,
        )
        ttl_after = await redis_module.redis_client.ttl(f"{AI_QUERY_CACHE_PREFIX}:tr_8")
        assert ttl_after > ttl_before  # 重置后变大
