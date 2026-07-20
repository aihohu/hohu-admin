"""容量鉴权三层（spec §6.4）

L1 用户写速率：Redis `ai:write:{user_id}` Sorted Set 滑动窗口（默认 20/min）
L2 用户日配额：Redis `ai:quota:{user_id}:{date}` UTC 日（默认 2000/day）
L3 单 tool 超时：asyncio.wait_for（默认 10s）

"写"判定：tool.meta.risk in ("high", "destructive") 或 hitl_always=True
risk="low" 的纯查询不计入 L1/L2（避免 user.list 几次就耗光配额）

计数策略（spec §6.4，2026-07-10 修订 S-11）：
  - perm 拒绝：在 L1/L2 计数 **之前** short-circuit → 不计数
  - data_scope 拒绝（业务函数内抛 AuthorizationException）：executor 捕获后
    必须 decr_quota() 回滚 L1/L2（之前已 INCR/ZADD，不回滚 = 偷用户配额）
  - 配额自身拒绝：check 函数在 raise 之前必须先回滚自身已写的计数
  - 业务异常 + 成功：计数保留（不回滚）
  - 超时（L3）：计为失败但保留计数

超管豁免（spec §6.4）：
  - L1/L2/L3 超管不豁免（防超管误用，与 §11.4 自动禁用一致）

修订记录：
  - 2026-07-10 S-7：L1 改 Sorted Set + Lua 脚本，原 INCR+EXPIRE 是固定窗口可被边界突发 2x 突破
  - 2026-07-10 S-8：L2 日期键改 UTC，TTL 算到当日 UTC 结束（原 date.today() 本地时区）
  - 2026-07-10 S-11：所有 raise 路径必须回滚已写计数（防用户被偷配额）
"""

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from app.core.exceptions import BusinessRuleException
from app.modules.ai.agents.tools.meta import AiToolMeta

logger = logging.getLogger(__name__)

# ============ 默认阈值（spec §6.4 / §11.2） ============
# 运行时从 sys_config 读，60s 缓存；这里仅作为 fallback default
DEFAULT_L1_RATE_PER_MIN = 20
DEFAULT_L2_DAILY_QUOTA = 2000
DEFAULT_L3_TIMEOUT_SEC = 10
L1_WINDOW_SEC = 60  # 滑窗 60s

# v1.5+ SR-19: 全局 L1 默认 0=不限（部署方按机器容量显式配）
DEFAULT_L1_GLOBAL_RATE_PER_MIN = 0

# sys_config 对应的 key（修订：从硬编码改为运行时可配）
_CFG_L1_RATE = "ai:rate_limit:user_write_per_min"
_CFG_L1_GLOBAL_RATE = "ai:rate_limit:global_per_min"  # v1.5+ SR-19
_CFG_L2_QUOTA = "ai:quota:daily_per_user"
_CFG_L3_TIMEOUT = "ai:limit:tool_timeout_sec"


async def _resolve_l1_limit() -> int:
    """从 sys_config 读 L1 速率上限（60s 缓存兜底，DB down 用 default）"""
    from app.db.session import AsyncSessionLocal  # noqa: PLC0415
    from app.modules.ai.agents.safety.ai_config import (  # noqa: PLC0415
        get_ai_config_int,
    )

    try:
        async with AsyncSessionLocal() as db:
            return await get_ai_config_int(db, _CFG_L1_RATE, DEFAULT_L1_RATE_PER_MIN)
    except Exception:
        return DEFAULT_L1_RATE_PER_MIN


async def _resolve_l2_limit() -> int:
    from app.db.session import AsyncSessionLocal  # noqa: PLC0415
    from app.modules.ai.agents.safety.ai_config import (  # noqa: PLC0415
        get_ai_config_int,
    )

    try:
        async with AsyncSessionLocal() as db:
            return await get_ai_config_int(db, _CFG_L2_QUOTA, DEFAULT_L2_DAILY_QUOTA)
    except Exception:
        return DEFAULT_L2_DAILY_QUOTA


async def _resolve_l3_timeout() -> int:
    from app.db.session import AsyncSessionLocal  # noqa: PLC0415
    from app.modules.ai.agents.safety.ai_config import (  # noqa: PLC0415
        get_ai_config_int,
    )

    try:
        async with AsyncSessionLocal() as db:
            return await get_ai_config_int(db, _CFG_L3_TIMEOUT, DEFAULT_L3_TIMEOUT_SEC)
    except Exception:
        return DEFAULT_L3_TIMEOUT_SEC


async def _resolve_l1_global_limit() -> int:
    """v1.5+ SR-19: 从 sys_config 读全局 L1 速率上限（默认 0=不限）"""
    from app.db.session import AsyncSessionLocal  # noqa: PLC0415
    from app.modules.ai.agents.safety.ai_config import (  # noqa: PLC0415
        get_ai_config_int,
    )

    try:
        async with AsyncSessionLocal() as db:
            return await get_ai_config_int(
                db, _CFG_L1_GLOBAL_RATE, DEFAULT_L1_GLOBAL_RATE_PER_MIN
            )
    except Exception:
        return DEFAULT_L1_GLOBAL_RATE_PER_MIN


# ============ Redis key 命名（spec §6.4） ============
_KEY_L1 = "ai:write:{user_id}"  # Sorted Set：member=call_uid, score=ts
_KEY_L1_GLOBAL = "ai:rate:global"  # v1.5+ SR-19 全局速率（不分 user_id）
_KEY_L2 = "ai:quota:{user_id}:{date}"  # UTC 日，TTL 到当日 UTC 结束
_KEY_L2_AGENT = "ai:quota:{user_id}:{agent_code}:{date}"  # v1.5+ SR-16 per-agent 维度


# Lua 脚本：原子化滑窗（修订 S-7）
# KEYS[1] = zset key
# ARGV[1] = window_start_ts (now - 60)
# ARGV[2] = now_ts
# ARGV[3] = unique member (uuid 防覆盖)
# ARGV[4] = window_sec (60)
# 返回当前窗口内的成员数（ZCARD）
_L1_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
local count = redis.call('ZCARD', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return count
"""


def is_write_tool(meta: AiToolMeta) -> bool:
    """spec §6.4: "写"判定

    risk="low" 的纯查询不计入 L1/L2（避免 user.list 几次就耗光配额）
    """
    return meta.risk in ("high", "destructive") or meta.hitl_always


async def check_l1_rate_limit(
    redis: Redis,
    user_id: int,
    *,
    limit: int | None = None,
) -> tuple[int, str]:
    """L1 用户写速率：滑动 60s 窗口（默认 20/min）— 修订 S-7

    实现：Redis Sorted Set + Lua 脚本，原子化执行：
      1. ZREMRANGEBYSCORE 清掉过期成员（窗口前）
      2. ZADD 当前调用（score=now, member=uuid 防覆盖）
      3. ZCARD 数窗口内成员
      4. EXPIRE 60s

    Returns:
        (count, member) — member 供调用方在 raise AuthorizationException 时
        通过 decr_quota(redis, user_id, l1_member=member) 精确回滚

    Raises:
        BusinessRuleException(AI_RATE_LIMIT_USER_WRITE) — 计数超 limit

    修订 S-11：超限时先 ZREM 自身再加回队列，再抛错。
    """
    if limit is None:
        limit = await _resolve_l1_limit()
    key = _KEY_L1.format(user_id=user_id)
    now = time.time()
    window_start = now - L1_WINDOW_SEC
    member = f"{now:.6f}:{uuid.uuid4().hex}"

    count = await redis.eval(_L1_LUA, 1, key, window_start, now, member, L1_WINDOW_SEC)
    count_int = int(count)

    if count_int > limit:
        # 修订 S-11：配额自身拒绝时 ZREM 自身，防用户被偷配额
        await redis.zrem(key, member)
        logger.info(
            "L1 rate limit exceeded",
            extra={"user_id": user_id, "current": count_int, "limit": limit},
        )
        from app.modules.ai.metrics import record_quota_rejected  # noqa: PLC0415

        record_quota_rejected("l1_rate")
        raise BusinessRuleException(
            f"用户写速率超限（{count_int}/{limit} per minute）",
            error_code="AI_RATE_LIMIT_USER_WRITE",
        )

    return count_int, member


async def check_l1_global_rate_limit(
    redis: Redis,
    *,
    limit: int | None = None,
) -> tuple[int, str] | None:
    """L1 全局速率：全系统写/分钟（v1.5+ SR-19）

    limit=0 / None（默认）→ 跳过检查（向后兼容，部署方未配时不防护）。
    limit>0 时用与用户级 L1 相同的 ZSET + Lua 滑窗。

    Returns:
        (count, member) — member 供调用方在 raise AuthorizationException 时
        通过 decr_quota(redis, user_id, l1_global_member=member) 精确回滚。
        limit=0 时返回 None（跳过检查，调用方据此不传 l1_global_member）。

    Raises:
        BusinessRuleException(AI_RATE_LIMIT_GLOBAL) — 全局计数超 limit
    """
    if limit is None:
        limit = await _resolve_l1_global_limit()
    if limit <= 0:
        return None  # 未配置全局限制

    key = _KEY_L1_GLOBAL
    now = time.time()
    window_start = now - L1_WINDOW_SEC
    member = f"{now:.6f}:{uuid.uuid4().hex}"

    count = await redis.eval(_L1_LUA, 1, key, window_start, now, member, L1_WINDOW_SEC)
    count_int = int(count)

    if count_int > limit:
        # 修订 S-11：超限时 ZREM 自身
        await redis.zrem(key, member)
        logger.info(
            "L1 global rate limit exceeded",
            extra={"current": count_int, "limit": limit},
        )
        from app.modules.ai.metrics import record_quota_rejected  # noqa: PLC0415

        record_quota_rejected("l1_global_rate")
        raise BusinessRuleException(
            f"系统繁忙，全局写速率超限（{count_int}/{limit} per minute），请稍后重试",
            error_code="AI_RATE_LIMIT_GLOBAL",
        )

    return count_int, member


async def check_l2_daily_quota(
    redis: Redis,
    user_id: int,
    *,
    limit: int | None = None,
) -> int:
    """L2 用户日配额：UTC 日（默认 2000/day）— 修订 S-8

    实现：
      - date key 用 UTC（原 date.today() 是本地时区，跨国部署翻转点不一致）
      - TTL 算到当日 UTC 结束（原固定 86400s 跨日累积偏差）
      - 仅 INCR 返回 1 时设 EXPIRE，避免每次调用都重置 TTL

    Returns:
        当前计数（供 executor 在 raise 时回滚用）

    Raises:
        BusinessRuleException(AI_DAILY_QUOTA_EXHAUSTED) — 计数超 limit

    修订 S-11：超限时先 DECR 自身，再抛错。
    """
    if limit is None:
        limit = await _resolve_l2_limit()
    now = datetime.now(UTC)
    date_str = now.strftime("%Y%m%d")
    key = _KEY_L2.format(user_id=user_id, date=date_str)

    # pipeline 保证 INCR + 条件 EXPIRE 同连接顺序执行
    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.ttl(key)
    incr_result, ttl = await pipe.execute()

    if incr_result == 1 or ttl is None or ttl < 0:
        # 第一次写入 OR 防御性（key 已过期但 INCR 又生效的极端 race）
        seconds_to_midnight = 86400 - (now.hour * 3600 + now.minute * 60 + now.second)
        await redis.expire(key, seconds_to_midnight)

    if incr_result > limit:
        # 修订 S-11：配额自身拒绝时 DECR 自身
        await redis.decr(key)
        logger.info(
            "L2 daily quota exhausted",
            extra={"user_id": user_id, "current": incr_result, "limit": limit},
        )
        from app.modules.ai.metrics import record_quota_rejected  # noqa: PLC0415

        record_quota_rejected("l2_daily")
        raise BusinessRuleException(
            f"今日 AI 写操作配额已用尽（{incr_result}/{limit}）",
            error_code="AI_DAILY_QUOTA_EXHAUSTED",
        )

    return int(incr_result)


async def check_l2_agent_quota(
    redis: Redis,
    user_id: int,
    agent_code: str,
    *,
    limit: int | None,
) -> int | None:
    """L2 per-agent 维度（v1.5+ SR-16）：单 agent 不能独占全局配额

    叠加不替代全局 L2：executor 先 check_l2_daily_quota 再调本函数。
    limit=None 时跳过（agent.daily_quota_per_user 未配置），直接返回 None。

    Redis key 规则与全局 L2 一致（UTC date + TTL 到当日 UTC 结束），
    仅多 agent_code 段。

    Raises:
        BusinessRuleException(AI_DAILY_QUOTA_EXHAUSTED) — per-agent 计数超 limit

    修订 S-11 扩展：超限时 DECR 自身；decr_quota(agent_code=...) 同步回滚。
    """
    if limit is None:
        return None

    now = datetime.now(UTC)
    date_str = now.strftime("%Y%m%d")
    key = _KEY_L2_AGENT.format(user_id=user_id, agent_code=agent_code, date=date_str)

    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.ttl(key)
    incr_result, ttl = await pipe.execute()

    if incr_result == 1 or ttl is None or ttl < 0:
        seconds_to_midnight = 86400 - (now.hour * 3600 + now.minute * 60 + now.second)
        await redis.expire(key, seconds_to_midnight)

    if incr_result > limit:
        await redis.decr(key)
        logger.info(
            "L2 per-agent quota exhausted",
            extra={
                "user_id": user_id,
                "agent_code": agent_code,
                "current": incr_result,
                "limit": limit,
            },
        )
        from app.modules.ai.metrics import record_quota_rejected  # noqa: PLC0415

        record_quota_rejected("l2_agent")
        raise BusinessRuleException(
            f"Agent {agent_code} 今日配额已用尽（{incr_result}/{limit}）",
            error_code="AI_DAILY_QUOTA_EXHAUSTED",
        )

    return int(incr_result)


async def decr_quota(
    redis: Redis,
    user_id: int,
    *,
    agent_code: str | None = None,
    l1_member: str | None = None,
    l1_global_member: str | None = None,
) -> None:
    """业务函数内 AuthorizationException 时回滚 L1/L2 计数（修订 S-11）

    必须在 executor 捕获 AuthorizationException 路径调用，否则 data_scope
    拒绝会偷掉用户的 L1/L2 配额。

    v1.5+ SR-16 扩展：agent_code 非 None 时同步回滚 per-agent L2 key。
    executor 仅在 agent.daily_quota_per_user 非 None 时传 agent_code
    （未配置专属额度的 agent 不写 per-agent key，无需回滚）。

    v1.5+ SR-19 扩展：l1_global_member 非 None 时同步 ZREM 全局 L1 zset 成员。
    executor 仅在 sys_config.ai:rate_limit:global_per_min > 0 时传
    （未配置全局限制时不写 zset，无需回滚）。

    Args:
        agent_code: 当前会话 agent.code；None=不回滚 per-agent L2
        l1_member: check_l1_rate_limit 返回的 member；传此值可精确删除本次调用
                   的 zset 成员。若 None 则不回滚用户级 L1。
        l1_global_member: check_l1_global_rate_limit 返回的 member；None=不回滚全局 L1。
    """
    l1_key = _KEY_L1.format(user_id=user_id)
    if l1_member is not None:
        await redis.zrem(l1_key, l1_member)

    # v1.5+ SR-19: 全局 L1 ZREM（仅当配置了全局限制 + 本次写入了 member）
    if l1_global_member is not None:
        await redis.zrem(_KEY_L1_GLOBAL, l1_global_member)

    # L2 全局：DECR
    now = datetime.now(UTC)
    date_str = now.strftime("%Y%m%d")
    l2_key = _KEY_L2.format(user_id=user_id, date=date_str)
    await redis.decr(l2_key)

    # L2 per-agent：仅当 agent_code 非 None 时回滚（与 executor 配对）
    if agent_code is not None:
        l2_agent_key = _KEY_L2_AGENT.format(
            user_id=user_id, agent_code=agent_code, date=date_str
        )
        await redis.decr(l2_agent_key)


def get_l3_timeout(
    *,
    timeout_sec: int = DEFAULT_L3_TIMEOUT_SEC,
) -> timedelta:
    """L3 单 tool 超时：返回 timedelta 供 asyncio.wait_for 用"""
    return timedelta(seconds=timeout_sec)


async def with_l3_timeout(coro, *, timeout_sec: int | None = None):
    """L3 单 tool 超时包装

    spec §6.4 / §6.5：超时抛 BusinessRuleException(AI_TOOL_TIMEOUT)，
    Gateway Executor 捕获后转 ToolResult.failure
    """
    if timeout_sec is None:
        timeout_sec = await _resolve_l3_timeout()
    try:
        return await asyncio.wait_for(coro, timeout=timeout_sec)
    except TimeoutError as e:
        logger.warning("L3 tool timeout", extra={"timeout_sec": timeout_sec})
        raise BusinessRuleException(
            f"单 tool 执行超时（>{timeout_sec}s）",
            error_code="AI_TOOL_TIMEOUT",
        ) from e
