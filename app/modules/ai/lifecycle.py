"""AI lifecycle terminal cleanup orchestration."""

import asyncio
import logging
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException
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
from app.modules.ai.service.atomic_completion_service import (
    DB_TOOLS,
    FILE_TOOLS,
    atomic_completion_service,
    is_protocol_owner,
    is_registered_readonly_action,
)
from app.modules.ai.service.chat_run_service import (
    chat_run_finalizer,
    chat_run_guard,
)
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.auth.service import load_live_user_authority
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
    redis: Redis, *, platform: PlatformContext, expired_only: bool = False
) -> int:
    """Recover durable prepared actions; Redis loss never expires a valid pending."""
    _require_platform(platform)
    cleaned = 0
    pending_source_validity: dict[int, bool] = {}
    tenant_contexts: dict[int, TenantContext] = {}
    async with AsyncSessionLocal() as db:
        query = select(AiPreparedAction)
        if expired_only:
            query = query.where(
                or_(
                    and_(
                        AiPreparedAction.status
                        == PreparedActionStatus.PENDING_CONFIRMATION.value,
                        AiPreparedAction.expires_at <= datetime.now(UTC),
                    ),
                    AiPreparedAction.status == PreparedActionStatus.RUNNING.value,
                )
            )
        else:
            query = query.where(
                AiPreparedAction.status.in_(
                    (
                        PreparedActionStatus.PENDING_CONFIRMATION.value,
                        PreparedActionStatus.APPROVED.value,
                        PreparedActionStatus.RUNNING.value,
                    )
                )
            )
        actions = list((await db.execute(query)).scalars().all())
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
        readonly = bool(
            getattr(candidate, "execute_tool_name", None)
        ) and is_registered_readonly_action(candidate)
        if status == PreparedActionStatus.RUNNING:
            if getattr(
                candidate, "execute_tool_name", None
            ) in DB_TOOLS and is_protocol_owner(
                candidate.execution_owner, candidate.execute_tool_name
            ):
                from app.modules.ai.agents.gateway.atomic_execution import (  # noqa: PLC0415
                    reconcile_prepared_completion,
                )

                outcome = await reconcile_prepared_completion(
                    candidate.confirmation_id, tenant=tenant, allow_fence=True
                )
                if outcome.disposition != "unknown":
                    await _project_recovered_completion(outcome.action, tenant=tenant)
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
                            "recovered completion cache cleanup unavailable",
                            exc_info=True,
                        )
                    cleaned += int(outcome.changed)
            # Registered reads keep their lease-based transport recovery. FILE,
            # legacy writes and malformed markers provide no DB rollback proof.
            if not readonly:
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
                await cleanup_db.execute(
                    select(func.set_config("lock_timeout", "2s", True))
                )
                context = await prepared_action_service.lock_confirmation_context(
                    cleanup_db, confirmation_id=candidate.confirmation_id, tenant=tenant
                )
                if context is None:
                    continue
                current = context.action
                current_status = PreparedActionStatus(current.status)
                readonly = bool(
                    getattr(current, "execute_tool_name", None)
                ) and is_registered_readonly_action(current)
                lease_expires = getattr(current, "execution_lease_expires_at", None)
                if lease_expires is not None and lease_expires.tzinfo is None:
                    lease_expires = lease_expires.replace(tzinfo=UTC)
                readonly_lease_expired = readonly and (
                    lease_expires is None or lease_expires <= datetime.now(UTC)
                )
                if expired_only:
                    current_expires = current.expires_at
                    if current_expires.tzinfo is None:
                        current_expires = current_expires.replace(tzinfo=UTC)
                    pending_expired = (
                        current_status == PreparedActionStatus.PENDING_CONFIRMATION
                        and current_expires <= datetime.now(UTC)
                    )
                    if not pending_expired and not (
                        current_status == PreparedActionStatus.RUNNING
                        and readonly_lease_expired
                    ):
                        continue
                if current_status == PreparedActionStatus.PENDING_CONFIRMATION:
                    target = PreparedActionStatus.EXPIRED
                    if candidate_expires > datetime.now(
                        UTC
                    ) and not pending_source_validity.get(candidate.action_id, False):
                        error_code = "AI_PREPARED_ACTION_SOURCE_STALE"
                    else:
                        error_code = "AI_HITL_EXPIRED"
                elif current_status == PreparedActionStatus.APPROVED:
                    # An old approved label carries no new DB claim/rollback
                    # proof. Preserve it for reconciliation rather than fail it.
                    if not readonly:
                        continue
                    target = PreparedActionStatus.FAILED
                    error_code = "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
                elif current_status == PreparedActionStatus.RUNNING:
                    # A concurrent claim will be reconciled on the next scan,
                    # after these locks have been released.
                    if not readonly_lease_expired:
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
                            cleanup_db,
                            operation.log_id,
                            error_code=error_code,
                            tenant=tenant,
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


async def _project_recovered_completion(action, *, tenant: TenantContext) -> None:  # noqa: ANN001
    """Repair display from committed facts; optional display cannot change them."""
    try:
        async with asyncio.timeout(8), AsyncSessionLocal() as db, db.begin():
            completed = await atomic_completion_service.lock_terminal(
                db, action.confirmation_id, tenant=tenant
            )
            actor = await load_live_user_authority(db, tenant=tenant)
            current = completed.action
            if not await result_projection_service.authorize_result_projection(
                db,
                actor,
                owner_user_id=current.user_id,
                lineage=result_projection_service.lineage_from_record(current),
            ):
                return
            ok = current.status == "succeeded"
            await chat_run_finalizer.finalize_prepared_action(
                db,
                action=current,
                ok=ok,
                duration_ms=current.duration_ms or 0,
                result=current.result_data if ok else None,
                result_ui=current.result_ui if ok else None,
                error_code=None if ok else current.error_code,
                error_msg=None if ok else "操作未完成，业务修改已回滚。",
                tenant=tenant,
                locked_completion=completed,
            )
    except Exception:
        logger.warning("recovered completion display unavailable", exc_info=True)


async def run_pending_expiry_loop(*, platform: PlatformContext) -> None:
    """Close offline approvals after their deadline without touching execution."""
    from app.core.redis import redis_client  # noqa: PLC0415

    _require_platform(platform)
    while True:
        await asyncio.sleep(30)
        try:
            await cleanup_prepared_actions_on_startup(
                redis_client, platform=platform, expired_only=True
            )
        except Exception:
            logger.exception("pending confirmation expiry sweep failed; will retry")


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
        if pending.action_id is not None:
            # A durable confirmation owns its log and projection. The legacy
            # Redis-only finalizer must never infer its outcome from a lease.
            return
        if (operation_log is None and pending.tool_name in DB_TOOLS | FILE_TOOLS) or (
            operation_log is not None
            and not getattr(operation_log, "readonly_snapshot", False)
        ):
            # A best-effort legacy write log cannot prove completion on its own.
            return
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


async def _cache_terminal_is_verified(
    db: AsyncSession, prepared: AiPreparedAction, *, tenant: TenantContext
) -> bool:
    """Only complete, currently locked facts permit terminal cache cleanup."""
    try:
        if is_registered_readonly_action(prepared):
            return PreparedActionStatus(prepared.status).is_terminal
        if prepared.status in {"succeeded", "failed"}:
            if prepared.execute_tool_name not in DB_TOOLS | FILE_TOOLS:
                return False
            completed = await atomic_completion_service.lock_terminal(
                db, prepared.confirmation_id, tenant=tenant
            )
            return completed.action.status == "succeeded" or (
                completed.action.status == "failed"
                and completed.action.execute_tool_name in DB_TOOLS
            )
        if prepared.status not in {"rejected", "expired"}:
            return False
        await atomic_completion_service.lock_unexecuted_terminal(
            db, prepared.confirmation_id, tenant=tenant
        )
        return True
    except BusinessException:
        return False


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
                        if not await _cache_terminal_is_verified(
                            db, prepared, tenant=tenant
                        ):
                            continue
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
                    if (log is None and pending.tool_name in DB_TOOLS | FILE_TOOLS) or (
                        log is not None and not log.readonly_snapshot
                    ):
                        continue
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
