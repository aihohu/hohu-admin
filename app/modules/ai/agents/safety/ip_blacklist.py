"""IP 级自动拉黑 — spec §11.4

单 IP 1 小时内 AI 鉴权拒绝 ≥ threshold → 拉黑该 IP（复用现有 IP 黑名单）。
NAT 网络豁免：白名单（system_config.ai:ip_allowlist）中的 IP 命中阈值时只告警不拉黑
（企业办公网常全员走单一出口 IP，硬拉黑会误伤整个公司）。

Redis key 设计：
  - ai:perm_denied:ip:{ip}:{hour_bucket} — 1h 滑动计数（TTL 2h，跨小时查询用）
  - blacklist:{ip_hash} — 拉黑 flag（复用现有 IP 黑名单 key 命名）

阈值 / 白名单都从 sys_config 读，60s 缓存：
  - ai:auto_disable:perm_denied_per_hour (int, default 50)
  - ai:ip_allowlist (JSON string array)
"""

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.agents.safety.ai_config import get_ai_config_int, get_ai_config_str

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_PER_HOUR = 50
DEFAULT_TTL_SEC = 2 * 3600  # 2h

_CFG_THRESHOLD = "ai:auto_disable:perm_denied_per_hour"
_CFG_ALLOWLIST = "ai:ip_allowlist"

# 白名单缓存（避免每次 AI 鉴权都查 DB）
_allowlist_cache: tuple[list[str], float] | None = None
_ALLOWLIST_TTL_SEC = 60


def _hour_bucket(now: datetime | None = None) -> str:
    dt = now or datetime.now(UTC)
    return dt.strftime("%Y%m%d%H")


def _count_key(ip: str, hour_bucket: str) -> str:
    return f"ai:perm_denied:ip:{ip}:{hour_bucket}"


def _blacklist_key(ip: str) -> str:
    """复用现有 IP 黑名单 key 命名（与 auth 模块的 IP 黑名单一致）"""
    ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest()
    return f"blacklist:{ip_hash}"


async def _load_allowlist(db: AsyncSession) -> list[str]:
    """读 NAT 白名单（60s 缓存）

    Returns:
        IP 字符串列表（精确匹配，不做 CIDR）
    """
    global _allowlist_cache
    if _allowlist_cache is not None:
        cached_list, fetched_at = _allowlist_cache
        import time  # noqa: PLC0415

        if time.time() - fetched_at < _ALLOWLIST_TTL_SEC:
            return cached_list

    raw = await get_ai_config_str(db, _CFG_ALLOWLIST, "[]")
    parsed: list[str] = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            parsed = [str(x) for x in data if isinstance(x, str) and x.strip()]
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("invalid ai:ip_allowlist json", extra={"error": str(e)})

    import time  # noqa: PLC0415

    _allowlist_cache = (parsed, time.time())
    return parsed


def _invalidate_allowlist_cache() -> None:
    global _allowlist_cache
    _allowlist_cache = None


async def record_perm_denied(
    redis: Redis,
    db: AsyncSession,
    ip: str,
    *,
    duration_sec: int = DEFAULT_TTL_SEC,
) -> bool:
    """记录一次 AI 鉴权拒绝（perm_denied / data_scope_violation）

    Args:
        redis: redis client
        db: 用于查 sys_config 的 session
        ip: 客户端 IP

    Returns:
        True = 本次触发拉黑；False = 仅计数（未到阈值 / 在白名单）
    """
    if not ip:
        return False

    bucket = _hour_bucket()
    key = _count_key(ip, bucket)
    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, DEFAULT_TTL_SEC)

    threshold = await get_ai_config_int(db, _CFG_THRESHOLD, DEFAULT_THRESHOLD_PER_HOUR)
    if current < threshold:
        return False

    # 到阈值：检查白名单
    allowlist = await _load_allowlist(db)
    if ip in allowlist:
        logger.warning(
            "ip hit perm_denied threshold but in NAT allowlist, NOT blacklisted",
            extra={"ip": ip, "count": current, "threshold": threshold},
        )
        return False

    # 拉黑：写 blacklist:{ip_hash}，TTL 与现有 IP 黑名单约定一致（这里复用 2h）
    bl_key = _blacklist_key(ip)
    await redis.set(bl_key, "1", ex=duration_sec)
    logger.warning(
        "ip auto-blacklisted for mass permission denial",
        extra={
            "ip": ip,
            "count": current,
            "threshold": threshold,
            "duration_sec": duration_sec,
        },
    )
    return True


async def is_ip_blacklisted(redis: Redis, ip: str) -> bool:
    """检查 IP 是否在黑名单（auth 中间件调用）"""
    if not ip:
        return False
    return bool(await redis.exists(_blacklist_key(ip)))


async def unblacklist_ip(redis: Redis, ip: str) -> None:
    """管理员手动解除（运维 API 用）"""
    if not ip:
        return
    await redis.delete(_blacklist_key(ip))
    # 同步清所有 hour_bucket 计数器（best effort，SCAN 找）
    async for key in redis.scan_iter(match=f"ai:perm_denied:ip:{ip}:*", count=100):
        await redis.delete(key)


def _unused(_: Any) -> None:
    """避免 lint 报 unused import"""
    _ = _
