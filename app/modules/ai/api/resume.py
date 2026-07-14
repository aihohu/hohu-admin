"""SSE 流断流续传端点 — spec §3 v1.5+

Task 3: 错误路径骨架；Task 4 在锁获取成功后替换为完整 SSE 实现。

错误码（按出现顺序）：
  410 AI_RESUME_DISABLED         — 功能未启用 / memory 模式（强制 redis_pubsub）
  400 AI_RESUME_MISSING_ID       — Last-Event-ID 头 + query param 均缺
  404 AI_RESUME_NOT_FOUND        — Redis 中无 pending（已过期 / 未发起 / 重启清扫）
  403 AI_RESUME_FORBIDDEN        — 当前 user 非 pending.owner
  410 AI_RESUME_ALREADY_RESOLVED — pending.wake_action 已设（断流期间已确认/拒绝）
  422 AI_RESUME_TTL_TOO_SHORT    — TTL < 60s（执行完来不及回写结果）
  409 AI_RESUME_IN_PROGRESS      — owner 锁被其它 worker 持有（双执行兜底）

成功路径在 Task 4：
  - 构造 ConfirmationResumedEvent
  - emit resumed → hang → execute_tool → emit result/close
  - finally 释放 owner 锁（Lua compare-and-delete）
"""

import logging
import secrets

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.redis import redis_client
from app.db.session import get_db
from app.modules.ai.agents.hitl.constants import (
    AI_HITL_OWNER_LOCK_PREFIX,
    AI_HITL_OWNER_LOCK_TTL_SEC,
)
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()

# Task 4 在此插入完整 SSE 流（execute_tool + emit resumed/result）。Task 3
# 阶段抢到锁后立即释放并抛 AI_INTERNAL_ERROR 占位，所有 Task 3 测试在锁获取
# 之前 / 之后立即断言错误码，不会落到此分支。
_RELEASE_LOCK_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _set_exc_code(exc: BusinessRuleException, code: int) -> BusinessRuleException:
    """spec §9.6: BusinessRuleException 默认 code=400，手动改 code 返 409/410/422"""
    exc.code = code
    return exc


@router.get("", summary="SSE 流断流续传（HITL 期热接管）")
async def resume_chat(
    request: Request,
    confirmation_id_query: str | None = Query(
        default=None,
        alias="confirmation_id",
        description="调试后备（主推 Last-Event-ID 头）",
    ),
    db: AsyncSession = Depends(get_db),  # noqa: ARG001 (Task 4: SSE body uses it)
    current_user: User = Depends(get_current_user),
):
    """spec §3 v1.5+: SSE 续传入口

    读 confirmation_id 顺序：Last-Event-ID 头 > ?confirmation_id= query param
    """
    # 1. 功能开关 + 模式校验
    if not settings.AI_SSE_RESUME_ENABLED:
        raise _set_exc_code(
            BusinessRuleException(
                "SSE 续传功能未启用", error_code="AI_RESUME_DISABLED"
            ),
            410,
        )
    if settings.AI_HITL_MODE != "redis_pubsub":
        raise _set_exc_code(
            BusinessRuleException(
                "续传要求 redis_pubsub 模式", error_code="AI_RESUME_DISABLED"
            ),
            410,
        )

    # 2. 取 confirmation_id（标准协议头优先）
    confirmation_id = request.headers.get("last-event-id") or confirmation_id_query
    if not confirmation_id:
        raise BusinessRuleException(
            "缺少 confirmation_id（Last-Event-ID 头或 query param）",
            error_code="AI_RESUME_MISSING_ID",
        )

    # 3. 取 Redis pending → 校验 owner + 已 wake + TTL
    pending = await hitl_manager.get_pending(redis_client, confirmation_id)
    if pending is None:
        raise NotFoundException("HITL confirmation", error_code="AI_RESUME_NOT_FOUND")
    if pending.user_id != current_user.user_id:
        logger.warning(
            "resume owner mismatch: confirmation_id=%s pending_user=%d current_user=%d",
            confirmation_id,
            pending.user_id,
            current_user.user_id,
        )
        raise AuthorizationException(error_code="AI_RESUME_FORBIDDEN")
    if pending.wake_action is not None:
        logger.info(
            "resume already resolved: confirmation_id=%s wake_action=%s",
            confirmation_id,
            pending.wake_action,
        )
        raise _set_exc_code(
            BusinessRuleException(
                "HITL 已被处理（断流期间用户已确认/拒绝）",
                error_code="AI_RESUME_ALREADY_RESOLVED",
            ),
            410,
        )

    ttl_sec = await hitl_manager.ttl(redis_client, confirmation_id)
    if ttl_sec < 60:
        raise _set_exc_code(
            BusinessRuleException(
                f"HITL 确认窗口剩余 {ttl_sec}s，已不足 60s",
                error_code="AI_RESUME_TTL_TOO_SHORT",
            ),
            422,
        )

    # 4. 抢 owner 锁（防 worker A cancel 慢导致 worker B 双执行，spec §2.3）
    worker_token = secrets.token_urlsafe(16)
    lock_key = f"{AI_HITL_OWNER_LOCK_PREFIX}:{confirmation_id}"
    lock_ok = await redis_client.set(
        lock_key, worker_token, nx=True, ex=AI_HITL_OWNER_LOCK_TTL_SEC
    )
    if not lock_ok:
        logger.info(
            "resume lock contention: confirmation_id=%s worker_token=%s",
            confirmation_id,
            worker_token,
        )
        raise _set_exc_code(
            BusinessRuleException(
                "已有 worker 接管此 confirmation，请稍后重试",
                error_code="AI_RESUME_IN_PROGRESS",
            ),
            409,
        )

    # Task 4 在此插入：构造 SSE 流（emit resumed → hang → execute_tool → emit result）
    # 当前 Task 3 仅占位：释放锁并抛错（不应被命中，因为 Task 3 测试在锁后即返回）
    await redis_client.eval(_RELEASE_LOCK_LUA, 1, lock_key, worker_token)
    raise BusinessRuleException("续传端点未完成实现", error_code="AI_INTERNAL_ERROR")
