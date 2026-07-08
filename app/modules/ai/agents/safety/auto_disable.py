"""AI 用户级自动禁用 — spec §11.4

单用户 1 小时内 `injection_pattern_matched` ≥ 5 → 自动禁用该用户 AI 功能 24h
（Redis `ai:user_disabled:{user_id}` TTL 24h）。

**超管豁免**：超管命中只发告警，不禁用——防止攻击者诱导超主触发注入把超主
AI 锁死、运维无入口（spec §11.4 原文）。

未含（留 v2+）：
  - 单 IP mass_permission_denied 自动拉黑（依赖 system_config.ai:auto_disable:perm_denied_per_hour + ai:ip_allowlist NAT 豁免）
  - Prometheus 告警集成（ai_super_admin_injection_alert）

Redis key 设计：
  - `ai:injection:cnt:{user_id}:{hour_bucket}` — 1h 计数器，TTL 2h
  - `ai:user_disabled:{user_id}` — 禁用 flag，TTL 24h
"""

import logging
from datetime import UTC, datetime

from redis.asyncio import Redis

from app.core.rbac import is_super_admin
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)

# 阈值（spec §11.4 原文，硬编码 MVP，v2+ 走 system_config）
INJECTION_THRESHOLD_PER_HOUR = 5
DISABLE_DURATION_SEC = 24 * 3600  # 24h
INJECTION_COUNT_TTL_SEC = 2 * 3600  # 2h（计数器保留 2h 给窗口跨小时查询）


def _hour_bucket(now: datetime | None = None) -> str:
    """UTC 当前小时桶，格式 YYYYMMDDHH（用于 Redis key 计数隔离）"""
    dt = now or datetime.now(UTC)
    return dt.strftime("%Y%m%d%H")


def _count_key(user_id: int, hour_bucket: str) -> str:
    return f"ai:injection:cnt:{user_id}:{hour_bucket}"


def _disabled_key(user_id: int) -> str:
    return f"ai:user_disabled:{user_id}"


async def record_injection(redis: Redis, user: User) -> int:
    """记录一次 injection 命中，返回当前小时桶计数；超阈值且非超管 → 自动禁用 24h

    Args:
        redis: redis client
        user: 当前用户（用 user_id 计数，is_super_admin 决定豁免）

    Returns:
        当前小时桶的累计计数（用于日志 / 告警）
    """
    hour_bucket = _hour_bucket()
    count_key = _count_key(user.user_id, hour_bucket)
    current = await redis.incr(count_key)
    if current == 1:
        await redis.expire(count_key, INJECTION_COUNT_TTL_SEC)

    if current >= INJECTION_THRESHOLD_PER_HOUR:
        if is_super_admin(user):
            logger.warning(
                "super_admin injection threshold hit (NOT disabling)",
                extra={
                    "user_id": user.user_id,
                    "user_name": user.user_name,
                    "count": current,
                },
            )
            # 超管只告警，不禁用（spec §11.4）
        else:
            disabled_key = _disabled_key(user.user_id)
            already_disabled = await redis.get(disabled_key)
            await redis.set(disabled_key, "1", ex=DISABLE_DURATION_SEC)
            if not already_disabled:
                logger.warning(
                    "user auto-disabled for injection threshold",
                    extra={
                        "user_id": user.user_id,
                        "user_name": user.user_name,
                        "count": current,
                        "duration_sec": DISABLE_DURATION_SEC,
                    },
                )
    return current


async def check_user_disabled(redis: Redis, user_id: int) -> bool:
    """检查用户是否被自动禁用（用于 chat.py 入口短路）

    Returns:
        True = 用户被禁用，应短路返回 AI_USER_AUTO_DISABLED
        False = 正常
    """
    disabled_key = _disabled_key(user_id)
    return bool(await redis.exists(disabled_key))
