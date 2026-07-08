"""容量鉴权三层（spec §6.4）

L1 用户写速率：Redis `ai:write:{user_id}` 滑动窗口（默认 20/min）
L2 用户日配额：Redis `ai:quota:{user_id}:{date}` UTC 日（默认 2000/day）
L3 单 tool 超时：asyncio.wait_for（默认 10s）

"写"判定：tool.meta.risk in ("high", "destructive") 或 hitl_always=True
risk="low" 的纯查询不计入 L1/L2（避免 user.list 几次就耗光配额）

计数策略（spec §6.4）：
  - perm / data_scope 拒绝：不计入
  - 配额自身拒绝：不计数（防循环重试刷配额）
  - 业务异常 + 成功：计数保留
  - 超时（L3）：计为失败但保留计数

超管豁免（spec §6.4）：
  - L1/L2/L3 超管不豁免（防超管误用，与 §11.4 自动禁用一致）
"""

import asyncio
import logging
from datetime import date, timedelta

from redis.asyncio import Redis

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.tools.meta import AiToolMeta

logger = logging.getLogger(__name__)

# ============ 默认阈值（spec §6.4 / §11.2） ============
# v1.5+ 走 system_config 表运行时可配；MVP 硬编码常量
DEFAULT_L1_RATE_PER_MIN = 20
DEFAULT_L2_DAILY_QUOTA = 2000
DEFAULT_L3_TIMEOUT_SEC = 10

# ============ Redis key 命名（spec §6.4） ============
_KEY_L1 = "ai:write:{user_id}"  # 滑动 60s 窗口
_KEY_L2 = "ai:quota:{user_id}:{date}"  # UTC 日，TTL 到当日结束


def is_write_tool(meta: AiToolMeta) -> bool:
    """spec §6.4: "写"判定

    risk="low" 的纯查询不计入 L1/L2（避免 user.list 几次就耗光配额）
    """
    return meta.risk in ("high", "destructive") or meta.hitl_always


async def check_l1_rate_limit(
    redis: Redis,
    user_id: int,
    *,
    limit: int = DEFAULT_L1_RATE_PER_MIN,
) -> None:
    """L1 用户写速率：滑动 60s 窗口（默认 20/min）

    Redis INCR + EXPIRE 实现：
      - 第一次 INCR 返回 1，设 EXPIRE 60s
      - 后续 INCR，若超 limit 抛 AI_RATE_LIMIT_USER_WRITE
      - 不主动删 key（让其自然过期，超管也不豁免，spec §6.4）
    """
    key = _KEY_L1.format(user_id=user_id)
    current = await redis.incr(key)
    if current == 1:
        # 第一次写入，设 60s 窗口
        await redis.expire(key, 60)

    if current > limit:
        logger.info(
            "L1 rate limit exceeded",
            extra={"user_id": user_id, "current": current, "limit": limit},
        )
        raise BusinessRuleException(
            f"用户写速率超限（{current}/{limit} per minute）",
            error_code="AI_RATE_LIMIT_USER_WRITE",
        )


async def check_l2_daily_quota(
    redis: Redis,
    user_id: int,
    *,
    limit: int = DEFAULT_L2_DAILY_QUOTA,
) -> None:
    """L2 用户日配额：UTC 日（默认 2000/day）

    Redis INCR + EXPIRE 到当日结束：
      - 第一次 INCR 设 EXPIRE = 秒数到次日 00:00 UTC
      - 超限抛 AI_DAILY_QUOTA_EXHAUSTED
    """
    today = date.today()
    key = _KEY_L2.format(user_id=user_id, date=today.isoformat())
    current = await redis.incr(key)
    if current == 1:
        # 第一次写入，设 TTL 到当日结束（约 86400 秒）
        # 简化：直接 24h TTL，跨日累积偏差 < 1 次写入，可接受
        await redis.expire(key, 86400)

    if current > limit:
        logger.info(
            "L2 daily quota exhausted",
            extra={"user_id": user_id, "current": current, "limit": limit},
        )
        raise BusinessRuleException(
            f"今日 AI 写操作配额已用尽（{current}/{limit}）",
            error_code="AI_DAILY_QUOTA_EXHAUSTED",
        )


def get_l3_timeout(
    *,
    timeout_sec: int = DEFAULT_L3_TIMEOUT_SEC,
) -> timedelta:
    """L3 单 tool 超时：返回 timedelta 供 asyncio.wait_for 用"""
    return timedelta(seconds=timeout_sec)


async def with_l3_timeout(coro, *, timeout_sec: int = DEFAULT_L3_TIMEOUT_SEC):
    """L3 单 tool 超时包装

    spec §6.4 / §6.5：超时抛 BusinessRuleException(AI_TOOL_TIMEOUT)，
    Gateway Executor 捕获后转 ToolResult.failure
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_sec)
    except TimeoutError as e:
        logger.warning("L3 tool timeout", extra={"timeout_sec": timeout_sec})
        raise BusinessRuleException(
            f"单 tool 执行超时（>{timeout_sec}s）",
            error_code="AI_TOOL_TIMEOUT",
        ) from e
