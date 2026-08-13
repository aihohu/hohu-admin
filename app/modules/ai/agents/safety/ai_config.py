"""AI 安全、配额与路由配置读取。

把硬编码常量改读 sys_config 表，60s 进程内缓存（参考 keyword_blocklist.py 模式）。

支持的 key：
  - ai:rate_limit:user_write_per_min      (int, default 20)        L1 用户写速率
  - ai:quota:daily_per_user               (int, default 2000)      L2 用户日配额
  - ai:limit:tool_timeout_sec             (int, default 10)        L3 单 tool 超时
  - ai:limit:max_history_messages         (int, default 50)        历史消息滑窗
  - ai:auto_disable:injection_per_hour    (int, default 5)         注入自动禁用阈值
  - ai:auto_disable:perm_denied_per_hour  (int, default 50)        IP 拉黑阈值
  - ai:auto_disable:duration_sec          (int, default 86400=24h) 自动禁用时长
  - ai:failures:threshold                 (int, default 2)         连续失败兜底阈值
  - ai:failures:ttl_sec                   (int, default 600=10min) 失败计数 TTL
  - ai:supervisor_enabled                 (bool, default True)     Supervisor 总开关
  - ai:supervisor_daily_limit             (int, default 100)       Supervisor LLM 日配额
  - ai:routing_legacy_null_mode           (bool, default False)    null 使用默认 agent_code

设计：
  - 模块级缓存（key → (value, fetched_at)），60s 自然过期
  - force_refresh=True 跳过缓存（管理员改配置后调）
  - sys_config 查询失败 / key 不存在 → 返回 default，不抛异常
  - invalidate_ai_config_cache() 清所有缓存（ConfigService.update 时调）
"""

import logging
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.system.service.config_service import config_service

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 60
_cache: dict[str, tuple[Any, float]] = {}


async def get_ai_config_int(
    db: AsyncSession,
    key: str,
    default: int,
    *,
    force_refresh: bool = False,
) -> int:
    """读 int 配置（缓存 60s）

    Args:
        db: 用于查 sys_config 的 session
        key: sys_config.config_key
        default: key 不存在 / 解析失败时返回的默认值
        force_refresh: True 跳过缓存

    Returns:
        int 配置值
    """
    cached = _cache.get(key)
    if not force_refresh and cached is not None:
        value, fetched_at = cached
        if time.time() - fetched_at < _CACHE_TTL_SEC:
            return value  # type: ignore[return-value]

    raw = await config_service.get_value(db, key)
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw)
        except (ValueError, TypeError):
            logger.warning(
                "invalid int for ai config, using default",
                extra={"key": key, "raw": raw, "default": default},
            )
            value = default

    _cache[key] = (value, time.time())
    return value


async def get_ai_config_str(
    db: AsyncSession,
    key: str,
    default: str,
    *,
    force_refresh: bool = False,
) -> str:
    """读 str 配置（缓存 60s）"""
    cached = _cache.get(key)
    if not force_refresh and cached is not None:
        value, fetched_at = cached
        if time.time() - fetched_at < _CACHE_TTL_SEC:
            return value  # type: ignore[return-value]

    raw = await config_service.get_value(db, key)
    value = raw if raw else default

    _cache[key] = (value, time.time())
    return value


async def get_ai_config_str_list(
    db: AsyncSession,
    key: str,
    default: list[str],
    *,
    force_refresh: bool = False,
) -> list[str]:
    """读取 JSON 数组配置并缓存 60 秒，供 ``ai:enabled_tools`` 等配置使用。

    sys_config 存的是 JSON 字符串（如 '["file.parse", "provider.export"]'）。
    解析失败 / 非 list / 元素非 str 时回退到 default（容错优先，不抛异常）。
    """
    cached = _cache.get(key)
    if not force_refresh and cached is not None:
        value, fetched_at = cached
        if time.time() - fetched_at < _CACHE_TTL_SEC:
            return value  # type: ignore[return-value]

    import json  # noqa: PLC0415

    raw = await config_service.get_value(db, key)
    value = default
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                value = parsed
            else:
                logger.warning(
                    "invalid JSON list for ai config, using default",
                    extra={"key": key, "raw": raw},
                )
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "JSON parse failed for ai config, using default",
                extra={"key": key, "raw": raw},
            )

    _cache[key] = (value, time.time())
    return value


async def get_ai_config_bool(
    db: AsyncSession,
    key: str,
    default: bool,
    *,
    force_refresh: bool = False,
) -> bool:
    """读 bool 配置（缓存 60s）.

    接受 'true' / '1' / 'yes'（大小写不敏感、自动 strip）→ True.
    其它值（含 'false' / '0' / 'no' / 非法字符串）→ False.
    sys_config 无值时 fallback default（通过 str(default).lower() 往返）.

    注意：与 get_ai_config_int 不同，非法值不 fallback default，而是返回 False
    （feature flag 安全侧倒：垃圾值 → 关闭功能）.
    """
    raw = await get_ai_config_str(
        db, key, default=str(default).lower(), force_refresh=force_refresh
    )
    return raw.strip().lower() in ("true", "1", "yes")


def invalidate_ai_config_cache(prefix: str = "ai:") -> None:
    """清缓存（ConfigService.update 改 ai:* 后调）

    Args:
        prefix: 清除以 prefix 开头的 key（默认所有 ai: 配置）
    """
    keys_to_remove = [k for k in _cache if k.startswith(prefix)]
    for k in keys_to_remove:
        _cache.pop(k, None)
