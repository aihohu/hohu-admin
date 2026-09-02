"""Supervisor LLM 路由日配额，独立于 PydanticAI UsageLimits。

Redis key: tenant:{tenant_id}:ai:supervisor:quota:{user_id}:{YYYY-MM-DD}，TTL 25h.
超限时跳过 LLM 路由直接 emit clarification_required.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.core.tenant import TenantContext
from app.modules.ai.agents.safety.ai_config import get_ai_config_int


def _utc_date() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


@dataclass
class QuotaResult:
    allowed: bool
    current_count: int
    daily_limit: int
    reason: str = ""


async def get_daily_count(r, user_id: int, *, tenant: TenantContext) -> int:
    """读当日已用次数."""
    key = f"tenant:{tenant.tenant_id}:ai:supervisor:quota:{user_id}:{_utc_date()}"
    raw = await r.get(key)
    return int(raw) if raw else 0


async def increment_daily_count(r, user_id: int, *, tenant: TenantContext) -> int:
    """原子 +1 并设 TTL，返回 increment 后的值."""
    key = f"tenant:{tenant.tenant_id}:ai:supervisor:quota:{user_id}:{_utc_date()}"
    pipe = r.pipeline(transaction=True)
    pipe.incr(key)
    pipe.expire(key, 25 * 3600, nx=True)
    new_count, _ = await pipe.execute()
    return int(new_count)


async def check_supervisor_quota(
    db: AsyncSession, *, user_id: int, tenant: TenantContext
) -> QuotaResult:
    """检查 Supervisor 日配额是否超限。"""
    daily_limit = await get_ai_config_int(
        db, "ai:supervisor_daily_limit", default=100, tenant=tenant
    )
    current = await get_daily_count(redis_client, user_id, tenant=tenant)
    if current >= daily_limit:
        return QuotaResult(
            allowed=False,
            current_count=current,
            daily_limit=daily_limit,
            reason="quota_exceeded",
        )
    return QuotaResult(allowed=True, current_count=current, daily_limit=daily_limit)
