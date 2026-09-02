"""supervisor_daily_limit Redis 计数器测试。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tenant_helpers import tenant_context

from app.modules.ai.agents.supervisor.quota import (
    check_supervisor_quota,
    increment_daily_count,
)

TENANT = tenant_context()


@pytest.mark.asyncio
async def test_quota_allows_under_limit():
    """默认 100/日，第 50 次 → allowed=True."""
    with (
        patch(
            "app.modules.ai.agents.supervisor.quota.get_daily_count",
            AsyncMock(return_value=50),
        ),
        patch(
            # patch quota 模块内的引用（不是 source module），否则已绑定的引用不受影响
            "app.modules.ai.agents.supervisor.quota.get_ai_config_int",
            AsyncMock(return_value=100),
        ),
    ):
        result = await check_supervisor_quota(AsyncMock(), user_id=1, tenant=TENANT)
    assert result.allowed is True


@pytest.mark.asyncio
async def test_quota_blocks_at_limit():
    """第 100 次 → allowed=False, reason='quota_exceeded'."""
    with (
        patch(
            "app.modules.ai.agents.supervisor.quota.get_daily_count",
            AsyncMock(return_value=100),
        ),
        patch(
            "app.modules.ai.agents.supervisor.quota.get_ai_config_int",
            AsyncMock(return_value=100),
        ),
    ):
        result = await check_supervisor_quota(AsyncMock(), user_id=1, tenant=TENANT)
    assert result.allowed is False
    assert result.reason == "quota_exceeded"


@pytest.mark.asyncio
async def test_quota_increment_after_check():
    """路由 LLM 调用前先递增计数，确保并发安全。"""
    fake_redis = MagicMock()
    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[51, True])
    fake_redis.pipeline.return_value = pipe

    # _utc_date 是函数，patch 必须给 return_value（不能直接 patch 成字符串）
    with patch(
        "app.modules.ai.agents.supervisor.quota._utc_date",
        return_value="2026-07-25",
    ):
        count = await increment_daily_count(fake_redis, user_id=1, tenant=TENANT)

    assert count == 51
    key = "tenant:0:ai:supervisor:quota:1:2026-07-25"
    fake_redis.pipeline.assert_called_once_with(transaction=True)
    pipe.incr.assert_called_once_with(key)
    pipe.expire.assert_called_once_with(
        key,
        25 * 3600,
        nx=True,
    )
