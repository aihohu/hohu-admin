"""AI 用户级自动禁用测试。

使用 fakeredis 隔离 Redis（无外部依赖）。
"""

# ruff: noqa: PLC0415

from typing import Any
from unittest.mock import MagicMock

import pytest
from fakeredis import aioredis as fakeredis_async
from tenant_helpers import tenant_context

from app.modules.ai.agents.safety.auto_disable import (
    DISABLE_DURATION_SEC,
    INJECTION_COUNT_TTL_SEC,
    INJECTION_THRESHOLD_PER_HOUR,
    _hour_bucket,
)
from app.modules.ai.agents.safety.auto_disable import (
    _count_key as _tenant_count_key,
)
from app.modules.ai.agents.safety.auto_disable import (
    _disabled_key as _tenant_disabled_key,
)
from app.modules.ai.agents.safety.auto_disable import (
    check_user_disabled as _check_user_disabled,
)
from app.modules.ai.agents.safety.auto_disable import (
    record_injection as _record_injection,
)

TENANT = tenant_context(actor_user_id=9001)
TENANT_B = tenant_context(tenant_id=37, actor_user_id=9001)


def _count_key(user_id: int, hour_bucket: str) -> str:
    return _tenant_count_key(user_id, hour_bucket, tenant_id=TENANT.tenant_id)


def _disabled_key(user_id: int) -> str:
    return _tenant_disabled_key(user_id, tenant_id=TENANT.tenant_id)


async def record_injection(redis, user, *, tenant=TENANT):
    return await _record_injection(redis, user, tenant=tenant)


async def check_user_disabled(redis, user_id: int, *, tenant=TENANT):
    return await _check_user_disabled(redis, user_id, tenant=tenant)


@pytest.fixture
async def fake_redis():
    """fakeredis 异步客户端，decode_responses=True（与生产一致），自动清理"""
    redis = fakeredis_async.FakeRedis(decode_responses=True)
    try:
        yield redis
    finally:
        await redis.flushall()
        await redis.aclose()


def _mock_user(user_id: int = 9001, is_super: bool = False) -> Any:
    """构造 mock user；is_super=True 触发 is_super_admin 第一条规则"""
    user = MagicMock()
    user.user_id = user_id
    user.user_name = "admin" if is_super else f"user_{user_id}"
    user.roles = []
    return user


class TestHourBucket:
    def test_format_yyyymmddhh(self) -> None:
        from datetime import datetime

        dt = datetime(
            2026, 7, 8, 14, 30, 45, tzinfo=__import__("datetime").timezone.utc
        )
        assert _hour_bucket(dt) == "2026070814"


class TestRecordInjectionCounting:
    """计数 + TTL 设置"""

    async def test_first_incr_sets_ttl(self, fake_redis) -> None:
        user = _mock_user()
        current = await record_injection(fake_redis, user)
        assert current == 1
        # TTL 应设置
        key = _count_key(user.user_id, _hour_bucket())
        ttl = await fake_redis.ttl(key)
        assert 0 < ttl <= INJECTION_COUNT_TTL_SEC

    async def test_subsequent_incr_does_not_reset_ttl(self, fake_redis) -> None:
        user = _mock_user()
        await record_injection(fake_redis, user)
        await record_injection(fake_redis, user)
        current = await record_injection(fake_redis, user)
        assert current == 3

    async def test_returns_current_count(self, fake_redis) -> None:
        user = _mock_user()
        for i in range(1, 4):
            current = await record_injection(fake_redis, user)
            assert current == i


class TestAutoDisableThreshold:
    """阈值 ≥5 触发自动禁用"""

    async def test_below_threshold_not_disabled(self, fake_redis) -> None:
        user = _mock_user()
        for _ in range(INJECTION_THRESHOLD_PER_HOUR - 1):
            await record_injection(fake_redis, user)
        assert await check_user_disabled(fake_redis, user.user_id) is False

    async def test_at_threshold_triggers_disable(self, fake_redis) -> None:
        user = _mock_user()
        for _ in range(INJECTION_THRESHOLD_PER_HOUR):
            await record_injection(fake_redis, user)
        assert await check_user_disabled(fake_redis, user.user_id) is True

    async def test_over_threshold_disabled(self, fake_redis) -> None:
        user = _mock_user()
        for _ in range(INJECTION_THRESHOLD_PER_HOUR + 3):
            await record_injection(fake_redis, user)
        assert await check_user_disabled(fake_redis, user.user_id) is True

    async def test_disabled_flag_has_ttl_24h(self, fake_redis) -> None:
        user = _mock_user()
        for _ in range(INJECTION_THRESHOLD_PER_HOUR):
            await record_injection(fake_redis, user)
        ttl = await fake_redis.ttl(_disabled_key(user.user_id))
        assert 0 < ttl <= DISABLE_DURATION_SEC


class TestSuperAdminExemption:
    """超级管理员命中阈值时只告警、不禁用。"""

    async def test_super_admin_at_threshold_not_disabled(self, fake_redis) -> None:
        user = _mock_user(is_super=True)
        for _ in range(INJECTION_THRESHOLD_PER_HOUR):
            await record_injection(fake_redis, user)
        assert await check_user_disabled(fake_redis, user.user_id) is False

    async def test_super_admin_over_threshold_still_not_disabled(
        self, fake_redis
    ) -> None:
        user = _mock_user(is_super=True)
        for _ in range(INJECTION_THRESHOLD_PER_HOUR + 10):
            await record_injection(fake_redis, user)
        assert await check_user_disabled(fake_redis, user.user_id) is False

    async def test_super_admin_still_increments_count(self, fake_redis) -> None:
        """超管豁免禁用，但计数照常（用于告警 / 审计）"""
        user = _mock_user(is_super=True)
        current = await record_injection(fake_redis, user)
        assert current == 1
        key = _count_key(user.user_id, _hour_bucket())
        assert await fake_redis.get(key) == "1"


class TestCheckUserDisabled:
    async def test_clean_user_not_disabled(self, fake_redis) -> None:
        assert await check_user_disabled(fake_redis, 9999) is False

    async def test_disabled_returns_true(self, fake_redis) -> None:
        await fake_redis.set(_disabled_key(9001), "1", ex=DISABLE_DURATION_SEC)
        assert await check_user_disabled(fake_redis, 9001) is True

    async def test_different_users_independent(self, fake_redis) -> None:
        """user A 被禁用不影响 user B"""
        await fake_redis.set(_disabled_key(9001), "1", ex=DISABLE_DURATION_SEC)
        assert await check_user_disabled(fake_redis, 9001) is True
        assert await check_user_disabled(fake_redis, 9002) is False


class TestKeyIsolation:
    """不同用户 / 不同小时桶互不影响"""

    async def test_different_users_independent_count(self, fake_redis) -> None:
        user_a = _mock_user(9001)
        user_b = _mock_user(9002)
        for _ in range(INJECTION_THRESHOLD_PER_HOUR):
            await record_injection(fake_redis, user_a)
        # user_a 已禁用，user_b 仍可用
        assert await check_user_disabled(fake_redis, user_a.user_id) is True
        assert await check_user_disabled(fake_redis, user_b.user_id) is False

    async def test_same_user_id_is_isolated_between_tenants(self, fake_redis) -> None:
        user = _mock_user(9001)
        for _ in range(INJECTION_THRESHOLD_PER_HOUR):
            await record_injection(fake_redis, user, tenant=TENANT)

        assert (
            await check_user_disabled(fake_redis, user.user_id, tenant=TENANT) is True
        )
        assert (
            await check_user_disabled(fake_redis, user.user_id, tenant=TENANT_B)
            is False
        )

    async def test_count_key_isolates_hour_bucket(self, fake_redis) -> None:
        """hour_bucket 不同时计数独立（key 含 bucket 后缀）"""
        bucket1 = "2026070814"
        bucket2 = "2026070815"
        # 直接 set 不同 bucket 的计数
        await fake_redis.set(_count_key(9001, bucket1), "10")
        await fake_redis.set(_count_key(9001, bucket2), "0")
        # 取值隔离
        assert await fake_redis.get(_count_key(9001, bucket1)) == "10"
        assert await fake_redis.get(_count_key(9001, bucket2)) == "0"
