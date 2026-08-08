"""AI lifecycle terminal cleanup orchestration."""

import logging
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.hitl.constants import (
    AI_CONFIRM_REDIS_PREFIX,
    AiOperationStatus,
    PreparedActionStatus,
)
from app.modules.ai.agents.hitl.manager import PendingPayload, hitl_manager
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.chat_run_service import (
    chat_run_finalizer,
    chat_run_guard,
)
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.prepared_action_service import prepared_action_service

logger = logging.getLogger(__name__)


async def cleanup_prepared_actions_on_startup(redis: Redis) -> int:
    """Recover durable prepared actions; Redis loss never expires a valid pending."""
    cleaned = 0
    async with AsyncSessionLocal() as db:
        actions = list(
            (
                await db.execute(
                    select(AiPreparedAction).where(
                        AiPreparedAction.status.in_(
                            (
                                PreparedActionStatus.PENDING_CONFIRMATION.value,
                                PreparedActionStatus.APPROVED.value,
                                PreparedActionStatus.RUNNING.value,
                            )
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
    for candidate in actions:
        candidate_expires = candidate.expires_at
        if candidate_expires.tzinfo is None:
            candidate_expires = candidate_expires.replace(tzinfo=UTC)
        status = PreparedActionStatus(candidate.status)
        if (
            status == PreparedActionStatus.PENDING_CONFIRMATION
            and candidate_expires > datetime.now(UTC)
        ):
            if candidate.guard_owner_token:
                try:
                    acquired = await chat_run_guard.acquire(
                        redis,
                        conversation_id=candidate.conversation_id,
                        owner_token=candidate.guard_owner_token,
                    )
                    if acquired:
                        await chat_run_guard.handoff_pending(
                            redis,
                            conversation_id=candidate.conversation_id,
                            owner_token=candidate.guard_owner_token,
                            confirmation_ttl_sec=max(
                                1,
                                int(
                                    (
                                        candidate_expires - datetime.now(UTC)
                                    ).total_seconds()
                                ),
                            ),
                        )
                except RedisError:
                    logger.warning(
                        "prepared guard rebuild skipped action_id=%s",
                        candidate.action_id,
                        exc_info=True,
                    )
            continue

        async with AsyncSessionLocal() as cleanup_db:
            async with cleanup_db.begin():
                current = await prepared_action_service.get_by_confirmation_id(
                    cleanup_db, candidate.confirmation_id
                )
                if current is None:
                    continue
                current_status = PreparedActionStatus(current.status)
                if current_status == PreparedActionStatus.PENDING_CONFIRMATION:
                    target = PreparedActionStatus.EXPIRED
                    error_code = "AI_HITL_EXPIRED"
                elif current_status in {
                    PreparedActionStatus.APPROVED,
                    PreparedActionStatus.RUNNING,
                }:
                    target = PreparedActionStatus.FAILED
                    error_code = "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
                else:
                    continue
                terminal = await prepared_action_service.transition_status(
                    cleanup_db,
                    action_id=current.action_id,
                    expected_status=current_status,
                    expected_version=current.row_version,
                    target_status=target,
                    error_code=error_code,
                )
                if terminal is None:
                    continue
                operation = await operation_log_service.get_by_tool_call_id(
                    cleanup_db,
                    terminal.execute_tool_call_id,
                    user_id=terminal.user_id,
                )
                if operation is not None:
                    operation_status = AiOperationStatus(operation.status)
                    if operation_status == AiOperationStatus.PENDING_CONFIRMATION:
                        await operation_log_service.mark_expired_if_pending(
                            cleanup_db, operation.log_id
                        )
                    elif operation_status == AiOperationStatus.RUNNING:
                        await operation_log_service.mark_failed(
                            cleanup_db,
                            operation.log_id,
                            error_code=error_code,
                            duration_ms=operation.duration_ms or 0,
                        )
                await chat_run_finalizer.finalize_prepared_action(
                    cleanup_db,
                    action=terminal,
                    ok=False,
                    error_code=error_code,
                    error_msg="确认已过期或执行被服务重启中断，请重新发起",
                )
        try:
            if candidate.guard_owner_token:
                await chat_run_guard.release(
                    redis,
                    conversation_id=candidate.conversation_id,
                    owner_token=candidate.guard_owner_token,
                )
            await hitl_manager.delete_pending(redis, candidate.confirmation_id)
        except RedisError:
            logger.warning(
                "prepared terminal cache cleanup skipped action_id=%s",
                candidate.action_id,
                exc_info=True,
            )
        cleaned += 1
    return cleaned


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

    cleaned = await cleanup_prepared_actions_on_startup(redis_client)
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
                    prepared = await prepared_action_service.get_by_confirmation_id(
                        db, confirmation_id
                    )
                    if (
                        prepared is not None
                        and prepared.status
                        == PreparedActionStatus.PENDING_CONFIRMATION.value
                        and (
                            prepared.expires_at.replace(tzinfo=UTC)
                            if prepared.expires_at.tzinfo is None
                            else prepared.expires_at
                        )
                        > datetime.now(UTC)
                    ):
                        continue
                    if prepared is not None:
                        await hitl_manager.delete_pending(redis_client, confirmation_id)
                        continue
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
