"""AI 用户级自动禁用。

单用户 1 小时内 `injection_pattern_matched` ≥ 5 → 自动禁用该用户 AI 功能 24h
（Redis `ai:user_disabled:{user_id}` TTL 24h）。

**超管豁免**：超管命中只发告警，不禁用——防止攻击者诱导超主触发注入把超主
系统必须保留运维解锁入口，避免用户被永久锁死。

未含（留 v2+）：
  - 单 IP mass_permission_denied 自动拉黑（依赖 system_config.ai:auto_disable:perm_denied_per_hour + ai:ip_allowlist NAT 豁免）
  - Prometheus 告警集成（ai_super_admin_injection_alert）

Redis key 设计：
  - `ai:tenant:{tenant_id}:injection:cnt:{user_id}:{hour_bucket}` — 1h 计数器
  - `ai:tenant:{tenant_id}:user_disabled:{user_id}` — 禁用 flag，TTL 24h
"""

import logging
from datetime import UTC, datetime

from redis.asyncio import Redis

from app.core.rbac import is_super_admin
from app.core.tenant import TenantContext
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)

# 自动禁用阈值。
# 运行时从 sys_config 读，60s 缓存；这里仅作为 fallback default
INJECTION_THRESHOLD_PER_HOUR = 5
DISABLE_DURATION_SEC = 24 * 3600  # 24h
INJECTION_COUNT_TTL_SEC = 2 * 3600  # 2h（计数器保留 2h 给窗口跨小时查询）

_CFG_THRESHOLD = "ai:auto_disable:injection_per_hour"
_CFG_DURATION = "ai:auto_disable:duration_sec"


async def _resolve_threshold(tenant: TenantContext) -> int:
    from app.db.session import AsyncSessionLocal  # noqa: PLC0415
    from app.modules.ai.agents.safety.ai_config import (  # noqa: PLC0415
        get_ai_config_int,
    )

    try:
        async with AsyncSessionLocal() as db:
            return await get_ai_config_int(
                db, _CFG_THRESHOLD, INJECTION_THRESHOLD_PER_HOUR, tenant=tenant
            )
    except Exception:
        return INJECTION_THRESHOLD_PER_HOUR


async def _resolve_duration(tenant: TenantContext) -> int:
    from app.db.session import AsyncSessionLocal  # noqa: PLC0415
    from app.modules.ai.agents.safety.ai_config import (  # noqa: PLC0415
        get_ai_config_int,
    )

    try:
        async with AsyncSessionLocal() as db:
            return await get_ai_config_int(
                db, _CFG_DURATION, DISABLE_DURATION_SEC, tenant=tenant
            )
    except Exception:
        return DISABLE_DURATION_SEC


def _hour_bucket(now: datetime | None = None) -> str:
    """UTC 当前小时桶，格式 YYYYMMDDHH（用于 Redis key 计数隔离）"""
    dt = now or datetime.now(UTC)
    return dt.strftime("%Y%m%d%H")


def _count_key(user_id: int, hour_bucket: str, *, tenant_id: int) -> str:
    return f"ai:tenant:{tenant_id}:injection:cnt:{user_id}:{hour_bucket}"


def _disabled_key(user_id: int, *, tenant_id: int) -> str:
    return f"ai:tenant:{tenant_id}:user_disabled:{user_id}"


async def record_injection(redis: Redis, user: User, *, tenant: TenantContext) -> int:
    """记录一次 injection 命中，返回当前小时桶计数；超阈值且非超管 → 自动禁用 24h

    Args:
        redis: redis client
        user: 当前用户（用 user_id 计数，is_super_admin 决定豁免）

    Returns:
        当前小时桶的累计计数（用于日志 / 告警）
    """
    hour_bucket = _hour_bucket()
    count_key = _count_key(user.user_id, hour_bucket, tenant_id=tenant.tenant_id)
    pipe = redis.pipeline(transaction=True)
    pipe.incr(count_key)
    pipe.expire(count_key, INJECTION_COUNT_TTL_SEC, nx=True)
    current, _ = await pipe.execute()
    current = int(current)

    if current >= await _resolve_threshold(tenant):
        if is_super_admin(user):
            logger.warning(
                "super_admin injection threshold hit (NOT disabling)",
                extra={
                    "user_id": user.user_id,
                    "user_name": user.user_name,
                    "count": current,
                },
            )
            # 超级管理员只告警，不自动禁用。
        else:
            disabled_key = _disabled_key(user.user_id, tenant_id=tenant.tenant_id)
            duration = await _resolve_duration(tenant)
            first_disable = await redis.set(disabled_key, "1", ex=duration, nx=True)
            if not first_disable:
                await redis.expire(disabled_key, duration)
            else:
                logger.warning(
                    "user auto-disabled for injection threshold",
                    extra={
                        "user_id": user.user_id,
                        "user_name": user.user_name,
                        "count": current,
                        "duration_sec": duration,
                    },
                )
                # 仅首次禁用时记录指标，避免重复统计 already_disabled。
                from app.modules.ai.metrics import (  # noqa: PLC0415
                    record_security_event,
                )

                record_security_event("auto_disable")
    return current


async def check_user_disabled(
    redis: Redis, user_id: int, *, tenant: TenantContext
) -> bool:
    """检查用户是否被自动禁用（用于 chat.py 入口短路）

    Returns:
        True = 用户被禁用，应短路返回 AI_USER_AUTO_DISABLED
        False = 正常
    """
    disabled_key = _disabled_key(user_id, tenant_id=tenant.tenant_id)
    return bool(await redis.exists(disabled_key))
