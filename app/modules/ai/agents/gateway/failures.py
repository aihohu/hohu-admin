"""连续失败兜底。

Redis 跨 /ai/chat 流持久化失败计数：
  - key: ai:tenant:{tenant_id}:failures:{user_id}:{tool_name}:{args_hash}
  - TTL: 600s（10min）
  - 同 (tool, args_hash) 连续失败 2 次 → 第 3 次直接抛 AI_REPEATED_FAILURE

为什么跨流持久化：
  完整版跨 /ai/chat 流不持久化，用户回答后 LLM 用相同 args 再调一次，
  计数从 0 开始，永远到不了第 3 次"切换引导模式"。
  Redis 跨流持久化让 LLM 知道"这个操作连续失败 2 次了，引导用户走传统界面"。

``args_hash`` 算法：
  sha256(json.dumps(args, sort_keys=True, default=_type_aware).encode()).hexdigest()

  default=lambda o: f"{type(o).__qualname__}:{o!r}" 给每个非 JSON 原生类型
  加类型名前缀，防 datetime(2026,1,1) 与字符串 "2026-01-01 00:00:00"
  产生相同 JSON 哈希碰撞（不同业务意图共享失败计数器）。

修订记录：
  - 2026-07-10 S-9：args_hash 改类型感知序列化
  - 2026-07-10 S-12：record_failure 仅在 INCR 返回 1 时设 EXPIRE，
    避免每次失败都重置 TTL 导致计数永不过期
"""

import hashlib
import json
import logging
from typing import Any

from redis.asyncio import Redis

from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext

logger = logging.getLogger(__name__)

# 连续失败两次后，第三次切换为引导模式。
# 运行时从 sys_config 读，60s 缓存；这里仅作为 fallback default
FAILURE_THRESHOLD = 2  # >= 2 时拦截
FAILURE_TTL_SEC = 600  # 10 min
_CFG_THRESHOLD = "ai:failures:threshold"
_CFG_TTL = "ai:failures:ttl_sec"


async def _resolve_threshold(tenant: TenantContext) -> int:
    from app.db.session import AsyncSessionLocal  # noqa: PLC0415
    from app.modules.ai.agents.safety.ai_config import (  # noqa: PLC0415
        get_ai_config_int,
    )

    try:
        async with AsyncSessionLocal() as db:
            return await get_ai_config_int(
                db, _CFG_THRESHOLD, FAILURE_THRESHOLD, tenant=tenant
            )
    except Exception:
        return FAILURE_THRESHOLD


async def _resolve_ttl(tenant: TenantContext) -> int:
    from app.db.session import AsyncSessionLocal  # noqa: PLC0415
    from app.modules.ai.agents.safety.ai_config import (  # noqa: PLC0415
        get_ai_config_int,
    )

    try:
        async with AsyncSessionLocal() as db:
            return await get_ai_config_int(db, _CFG_TTL, FAILURE_TTL_SEC, tenant=tenant)
    except Exception:
        return FAILURE_TTL_SEC


# 连续失败计数的 Redis key 前缀。
_KEY = "ai:tenant:{tenant_id}:failures:{user_id}:{tool_name}:{args_hash}"


def _type_aware_default(o: Any) -> str:
    """json.dumps default：类型感知序列化（修订 S-9）

    给非 JSON 原生类型加类型名前缀，防不同类型对象哈希碰撞：
      datetime(2026,1,1) → "datetime:datetime.datetime(2026, 1, 1, 0, 0)"
      str("2026-01-01 00:00:00") → 原生 JSON 字符串，无前缀
    两者 hash 不同，业务意图隔离。
    """
    return f"{type(o).__qualname__}:{o!r}"


def compute_args_hash(args: dict[str, Any]) -> str:
    """计算参数的稳定 SHA256 hash。

    sort_keys=True 保证字典顺序无关；default=_type_aware_default 给非 JSON
    原生类型加类型名前缀，防 datetime 与 string 等碰撞。
    """
    payload = json.dumps(
        args, sort_keys=True, default=_type_aware_default, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def check_repeated_failure(
    redis: Redis,
    user_id: int,
    tool_name: str,
    args_hash: str,
    *,
    tenant: TenantContext,
) -> None:
    """检查是否触发 ``AI_REPEATED_FAILURE``。

    在 execute_tool 入口处调用：若 Redis 已记录失败 >= FAILURE_THRESHOLD，
    抛 BusinessRuleException(AI_REPEATED_FAILURE)，Gateway 转 ToolResult 给 LLM
    """
    key = _KEY.format(
        tenant_id=tenant.tenant_id,
        user_id=user_id,
        tool_name=tool_name,
        args_hash=args_hash,
    )
    failures_str = await redis.get(key)
    failures = int(failures_str or 0)

    threshold = await _resolve_threshold(tenant)
    if failures >= threshold:
        logger.info(
            "repeated failure threshold hit",
            extra={
                "user_id": user_id,
                "tool": tool_name,
                "failures": failures,
                "args_hash": args_hash[:8],
            },
        )
        raise BusinessRuleException(
            f"相同操作已连续失败 {failures} 次，建议引导用户走传统界面",
            error_code="AI_REPEATED_FAILURE",
        )


async def record_failure(
    redis: Redis,
    user_id: int,
    tool_name: str,
    args_hash: str,
    *,
    tenant: TenantContext,
) -> None:
    """记录一次失败（INCR + 条件 EXPIRE，修订 S-12）

    仅在 INCR 返回 1（第一次失败）时设 EXPIRE，避免每次失败都重置 TTL
    导致缓慢累积的失败永不过期（用户被永久锁死该 (tool, args) 对）。
    """
    key = _KEY.format(
        tenant_id=tenant.tenant_id,
        user_id=user_id,
        tool_name=tool_name,
        args_hash=args_hash,
    )
    ttl = await _resolve_ttl(tenant)
    pipe = redis.pipeline(transaction=True)
    pipe.incr(key)
    pipe.expire(key, ttl, nx=True)
    failures, _ = await pipe.execute()
    failures = int(failures)
    logger.debug(
        "failure recorded",
        extra={
            "user_id": user_id,
            "tool": tool_name,
            "failures": failures,
            "args_hash": args_hash[:8],
        },
    )


async def clear_failures(
    redis: Redis,
    user_id: int,
    tool_name: str,
    args_hash: str,
    *,
    tenant: TenantContext,
) -> None:
    """成功后清零对应参数的失败计数。"""
    key = _KEY.format(
        tenant_id=tenant.tenant_id,
        user_id=user_id,
        tool_name=tool_name,
        args_hash=args_hash,
    )
    await redis.delete(key)
