"""连续失败兜底（spec §6.5）

Redis 跨 /ai/chat 流持久化失败计数：
  - key: ai:failures:{user_id}:{tool_name}:{args_hash}
  - TTL: 600s（10min）
  - 同 (tool, args_hash) 连续失败 2 次 → 第 3 次直接抛 AI_REPEATED_FAILURE

为什么跨流持久化（spec §6.5 关键变化）：
  完整版跨 /ai/chat 流不持久化，用户回答后 LLM 用相同 args 再调一次，
  计数从 0 开始，永远到不了第 3 次"切换引导模式"。
  Redis 跨流持久化让 LLM 知道"这个操作连续失败 2 次了，引导用户走传统界面"。

args_hash 算法（spec §6.5）：
  sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
"""

import hashlib
import json
import logging
from typing import Any

from redis.asyncio import Redis

from app.core.exceptions import BusinessRuleException

logger = logging.getLogger(__name__)

# spec §6.5：连续失败 2 次后第 3 次切换引导模式
FAILURE_THRESHOLD = 2  # >= 2 时拦截
FAILURE_TTL_SEC = 600  # 10 min

# spec §6.5 Redis key 命名
_KEY = "ai:failures:{user_id}:{tool_name}:{args_hash}"


def compute_args_hash(args: dict[str, Any]) -> str:
    """计算 args 的 SHA256 hash（spec §6.5）

    sort_keys=True 保证字典顺序无关，default=str 兼容不可序列化对象
    """
    payload = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


async def check_repeated_failure(
    redis: Redis,
    user_id: int,
    tool_name: str,
    args_hash: str,
) -> None:
    """检查是否触发 AI_REPEATED_FAILURE（spec §6.5）

    在 execute_tool 入口处调用：若 Redis 已记录失败 >= FAILURE_THRESHOLD，
    抛 BusinessRuleException(AI_REPEATED_FAILURE)，Gateway 转 ToolResult 给 LLM
    """
    key = _KEY.format(user_id=user_id, tool_name=tool_name, args_hash=args_hash)
    failures_str = await redis.get(key)
    failures = int(failures_str or 0)

    if failures >= FAILURE_THRESHOLD:
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
) -> None:
    """记录一次失败（INCR + EXPIRE）"""
    key = _KEY.format(user_id=user_id, tool_name=tool_name, args_hash=args_hash)
    failures = await redis.incr(key)
    await redis.expire(key, FAILURE_TTL_SEC)
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
) -> None:
    """成功路径清零失败计数（spec §6.5）"""
    key = _KEY.format(user_id=user_id, tool_name=tool_name, args_hash=args_hash)
    await redis.delete(key)
