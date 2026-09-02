"""AI lifecycle terminal cleanup orchestration."""

import logging
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import PlatformContext, TenantContext
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
from app.modules.system.models.tenant import Tenant

logger = logging.getLogger(__name__)


def _require_platform(platform: PlatformContext) -> None:
    if not isinstance(platform, PlatformContext):
        raise TypeError("platform context is required")


async def _tenant_context(
    db: AsyncSession,
    *,
    tenant_id: int,
    actor_user_id: int,
    platform: PlatformContext,
) -> TenantContext:
    _require_platform(platform)
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise RuntimeError(f"tenant {tenant_id} no longer exists")
    return TenantContext(
        tenant_id=tenant.tenant_id,
        tenant_code=tenant.tenant_code,
        actor_user_id=actor_user_id,
        tenant_version=tenant.row_version,
        source="platform_control",
    )


async def cleanup_prepared_actions_on_startup(
    redis: Redis, *, platform: PlatformContext
) -> int:
    """Recover durable prepared actions; Redis loss never expires a valid pending."""
    _require_platform(platform)
    cleaned = 0
    pending_source_validity: dict[int, bool] = {}
    tenant_contexts: dict[int, TenantContext] = {}
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
        for action in actions:
            tenant = await _tenant_context(
                db,
                tenant_id=action.tenant_id,
                actor_user_id=action.user_id,
                platform=platform,
            )
            tenant_contexts[action.action_id] = tenant
            action_expires = action.expires_at
            if action_expires.tzinfo is None:
                action_expires = action_expires.replace(tzinfo=UTC)
            if (
                action.status == PreparedActionStatus.PENDING_CONFIRMATION.value
                and action_expires > datetime.now(UTC)
            ):
                pending_source_validity[
                    action.action_id
                ] = await prepared_action_service.pending_source_is_valid(
                    db, action, tenant=tenant
                )
    for candidate in actions:
        tenant = tenant_contexts[candidate.action_id]
        candidate_expires = candidate.expires_at
        if candidate_expires.tzinfo is None:
            candidate_expires = candidate_expires.replace(tzinfo=UTC)
        status = PreparedActionStatus(candidate.status)
        if status == PreparedActionStatus.RUNNING:
            candidate_lease = candidate.execution_lease_expires_at
            if candidate_lease is not None:
                if candidate_lease.tzinfo is None:
                    candidate_lease = candidate_lease.replace(tzinfo=UTC)
                if candidate_lease > datetime.now(UTC):
                    continue
        if (
            status == PreparedActionStatus.PENDING_CONFIRMATION
            and candidate_expires > datetime.now(UTC)
            and pending_source_validity.get(candidate.action_id, False)
        ):
            if candidate.guard_owner_token:
                try:
                    acquired = await chat_run_guard.acquire(
                        redis,
                        conversation_id=candidate.conversation_id,
                        owner_token=candidate.guard_owner_token,
                        tenant=tenant,
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
                            tenant=tenant,
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
                    cleanup_db, candidate.confirmation_id, tenant=tenant
                )
                if current is None:
                    continue
                current_status = PreparedActionStatus(current.status)
                if current_status == PreparedActionStatus.PENDING_CONFIRMATION:
                    target = PreparedActionStatus.EXPIRED
                    if candidate_expires > datetime.now(
                        UTC
                    ) and not pending_source_validity.get(candidate.action_id, False):
                        error_code = "AI_PREPARED_ACTION_SOURCE_STALE"
                    else:
                        error_code = "AI_HITL_EXPIRED"
                elif current_status == PreparedActionStatus.APPROVED:
                    target = PreparedActionStatus.FAILED
                    error_code = "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
                elif current_status == PreparedActionStatus.RUNNING:
                    lease_expires = current.execution_lease_expires_at
                    if lease_expires is not None:
                        if lease_expires.tzinfo is None:
                            lease_expires = lease_expires.replace(tzinfo=UTC)
                        if lease_expires > datetime.now(UTC):
                            continue
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
                    execution_lease_not_after=(
                        datetime.now(UTC)
                        if current_status == PreparedActionStatus.RUNNING
                        else None
                    ),
                    tenant=tenant,
                )
                if terminal is None:
                    continue
                operation = await operation_log_service.get_by_tool_call_id(
                    cleanup_db,
                    terminal.execute_tool_call_id,
                    user_id=terminal.user_id,
                    tenant=tenant,
                )
                if operation is not None:
                    operation_status = AiOperationStatus(operation.status)
                    if operation_status == AiOperationStatus.PENDING_CONFIRMATION:
                        await operation_log_service.mark_expired_if_pending(
                            cleanup_db, operation.log_id, tenant=tenant
                        )
                    elif operation_status == AiOperationStatus.RUNNING:
                        await operation_log_service.mark_failed(
                            cleanup_db,
                            operation.log_id,
                            error_code=error_code,
                            duration_ms=operation.duration_ms or 0,
                            tenant=tenant,
                        )
                await chat_run_finalizer.finalize_prepared_action(
                    cleanup_db,
                    action=terminal,
                    ok=False,
                    error_code=error_code,
                    error_msg="确认已过期或执行被服务重启中断，请重新发起",
                    tenant=tenant,
                )
        try:
            if candidate.guard_owner_token:
                await chat_run_guard.release(
                    redis,
                    conversation_id=candidate.conversation_id,
                    owner_token=candidate.guard_owner_token,
                    tenant=tenant,
                )
            await hitl_manager.delete_pending(
                redis, candidate.confirmation_id, tenant=tenant
            )
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
    tenant: TenantContext,
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
                    db, operation_log.log_id, tenant=tenant
                )
                error_code = "AI_HITL_EXPIRED"
                error_msg = "确认等待因服务重启而过期，请重新发起"
            elif status == AiOperationStatus.RUNNING:
                await operation_log_service.mark_failed(
                    db,
                    operation_log.log_id,
                    error_code=error_code,
                    duration_ms=operation_log.duration_ms or 0,
                    tenant=tenant,
                )
            else:
                existing_message_id = await db.scalar(
                    select(AiMessage.message_id)
                    .where(
                        AiMessage.tenant_id == tenant.tenant_id,
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
                tenant=tenant,
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
            tenant=tenant,
        )
    await hitl_manager.delete_pending(redis, confirmation_id, tenant=tenant)


async def cleanup_orphaned_pending_on_startup(*, platform: PlatformContext) -> int:
    """收口 memory-mode 重启遗留；失败项保留到 Redis TTL，不伪装已清理。"""
    from app.core.redis import redis_client  # noqa: PLC0415

    _require_platform(platform)
    cleaned = await cleanup_prepared_actions_on_startup(redis_client, platform=platform)
    pattern = f"{AI_CONFIRM_REDIS_PREFIX}:tenant:*"
    try:
        async for key in redis_client.scan_iter(match=pattern, count=100):
            key_text = key.decode() if isinstance(key, bytes) else str(key)
            try:
                confirmation_id = key_text.rsplit(":", 1)[-1]
                tenant_id = int(key_text.rsplit(":", 2)[-2])
                if tenant_id < 0:
                    raise ValueError("negative tenant ID")
            except (IndexError, ValueError):
                logger.warning("startup lifecycle skipped malformed HITL key")
                continue
            pending = await hitl_manager.get_pending_for_platform(
                redis_client,
                confirmation_id,
                tenant_id=tenant_id,
                platform=platform,
            )
            if pending is None:
                continue
            try:
                async with AsyncSessionLocal() as db:
                    tenant = await _tenant_context(
                        db,
                        tenant_id=pending.tenant_id,
                        actor_user_id=pending.user_id,
                        platform=platform,
                    )
                    prepared = await prepared_action_service.get_by_confirmation_id(
                        db, confirmation_id, tenant=tenant
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
                        await hitl_manager.delete_pending(
                            redis_client, confirmation_id, tenant=tenant
                        )
                        continue
                    log = await operation_log_service.get_by_tool_call_id(
                        db,
                        pending.tool_call_id,
                        user_id=pending.user_id,
                        tenant=tenant,
                    )
                    await finalize_orphaned_pending(
                        db,
                        redis_client,
                        confirmation_id=confirmation_id,
                        pending=pending,
                        operation_log=log,
                        tenant=tenant,
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


async def cleanup_durable_prepared_actions_on_startup(
    *, platform: PlatformContext
) -> int:
    """Recover durable state in every HITL mode without scanning legacy cache."""
    from app.core.redis import redis_client  # noqa: PLC0415

    return await cleanup_prepared_actions_on_startup(redis_client, platform=platform)
