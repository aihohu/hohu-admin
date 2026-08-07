"""AI lifecycle terminal cleanup orchestration."""

import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.hitl.constants import (
    AI_CONFIRM_REDIS_PREFIX,
    AiOperationStatus,
)
from app.modules.ai.agents.hitl.manager import PendingPayload, hitl_manager
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.service.chat_run_service import (
    chat_run_finalizer,
    chat_run_guard,
)
from app.modules.ai.service.operation_log_service import operation_log_service

logger = logging.getLogger(__name__)


async def finalize_orphaned_pending(
    db: AsyncSession,
    redis: Redis,
    *,
    confirmation_id: str,
    pending: PendingPayload,
    operation_log: AiOperationLog | None,
) -> None:
    """先 commit terminal operation/projection，再由原 owner 释放 guard。"""
    try:
        should_finalize = True
        ok = False
        result = None
        error_code = "AI_HITL_EXECUTION_INTERRUPTED"
        error_msg = "服务重启导致确认执行中断，请重新发起"
        if operation_log is not None:
            status = AiOperationStatus(operation_log.status)
            if status == AiOperationStatus.PENDING_CONFIRMATION:
                await operation_log_service.mark_expired_if_pending(
                    db, operation_log.log_id
                )
                error_code = "AI_HITL_EXPIRED"
                error_msg = "确认等待因服务重启而过期，请重新发起"
            elif status == AiOperationStatus.RUNNING:
                await operation_log_service.mark_failed(
                    db,
                    operation_log.log_id,
                    error_code=error_code,
                    duration_ms=operation_log.duration_ms or 0,
                )
            else:
                existing_message_id = await db.scalar(
                    select(AiMessage.message_id)
                    .where(
                        AiMessage.conversation_id == pending.conversation_id,
                        AiMessage.role == "assistant",
                        AiMessage.trace_id == pending.trace_id,
                    )
                    .limit(1)
                )
                should_finalize = existing_message_id is None
                ok = status == AiOperationStatus.SUCCESS
                result = (
                    {"summary": operation_log.result_summary}
                    if ok and operation_log.result_summary
                    else None
                )
                if status == AiOperationStatus.REJECTED:
                    error_code = "USER_REJECTED"
                    error_msg = "用户已取消此操作"
                elif status == AiOperationStatus.EXPIRED:
                    error_code = "AI_HITL_EXPIRED"
                    error_msg = "确认已过期，请重新发起"
                elif status == AiOperationStatus.FAILED:
                    error_code = operation_log.error_code or "AI_INTERNAL_ERROR"
                    error_msg = "工具执行失败，请重新发起"
                else:
                    error_code = None
                    error_msg = None
        if should_finalize:
            await chat_run_finalizer.finalize_pending_turn(
                db,
                pending=pending,
                ok=ok,
                result=result,
                error_code=error_code,
                error_msg=error_msg,
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    if pending.guard_owner_token:
        await chat_run_guard.release(
            redis,
            conversation_id=pending.conversation_id,
            owner_token=pending.guard_owner_token,
        )
    await hitl_manager.delete_pending(redis, confirmation_id)


async def cleanup_orphaned_pending_on_startup() -> int:
    """收口 memory-mode 重启遗留；失败项保留到 Redis TTL，不伪装已清理。"""
    from app.core.redis import redis_client  # noqa: PLC0415

    cleaned = 0
    pattern = f"{AI_CONFIRM_REDIS_PREFIX}:*"
    try:
        async for key in redis_client.scan_iter(match=pattern, count=100):
            key_text = key.decode() if isinstance(key, bytes) else str(key)
            confirmation_id = key_text.rsplit(":", 1)[-1]
            pending = await hitl_manager.get_pending(redis_client, confirmation_id)
            if pending is None:
                continue
            try:
                async with AsyncSessionLocal() as db:
                    log = await operation_log_service.get_by_tool_call_id(
                        db,
                        pending.tool_call_id,
                        user_id=pending.user_id,
                    )
                    await finalize_orphaned_pending(
                        db,
                        redis_client,
                        confirmation_id=confirmation_id,
                        pending=pending,
                        operation_log=log,
                    )
                cleaned += 1
            except Exception:
                logger.exception(
                    "startup pending terminal cleanup failed",
                    extra={"confirmation_id": confirmation_id},
                )
    except RedisError:
        logger.warning(
            "startup pending scan skipped because Redis is unavailable",
            exc_info=True,
        )
    return cleaned
