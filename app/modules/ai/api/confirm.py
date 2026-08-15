"""HITL 确认端点。

POST /ai/confirm
  body: {confirmationId, action}
  → 200 {code: 200, msg: "success", data: {toolCallId, status: "queued"}}
  → 200 {code: 200, msg: "success", data: {toolCallId, status: "stream_gone"}}
       （修订 S-14：wake 返回 False 时返回此 status，前端立即停止轮询）
  → 403 非 owner（NOT_CONFIRMATION_OWNER）
  → 403 用户被自动禁用（AI_USER_DISABLED，修订 S-13）
  → 404 confirmation 不存在/已过期（CONFIRMATION_EXPIRED_OR_NOT_FOUND）

工作流：
  1. 从 Redis 取 pending payload
  2. owner 校验（user_id 必须匹配）
  3. **修订 S-13**：check_user_disabled（HITL 期间用户被自动禁用则阻断）
  4. 写 ai_operation_log.approved_by（审计追责，无论 stream 是否还在）
  5. wake 进程内 asyncio.Event
  6. **修订 S-14**：wake 返回 False 时 → mark_expired_if_pending + 返回
     status="stream_gone"（前端停止轮询，提示用户重新发起）

修订记录：
  - HITL 期间用户被自动禁用后仍可调用 POST /ai/confirm
    执行破坏性操作 → 加 check_user_disabled 阻断
  - 2026-07-10 S-14：wake 失败时返回 200+queued 误导前端轮询 30s → 改返回
    status="stream_gone" + mark_expired_if_pending 兜底审计；wake 实现要求
    通过原子状态迁移和唤醒载荷消费防止双击竞态
"""

import asyncio
import logging
import secrets
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import ensure_ai_chat_use
from app.core.base_response import ResponseModel
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.redis import redis_client
from app.core.tenant import resolve_tenant_id
from app.db.session import AsyncSessionLocal, get_db
from app.modules.ai.agents.gateway.executor import (
    execute_approved_prepared_action,
    validate_prepared_execution,
)
from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.agents.hitl.constants import ConfirmAction, PreparedActionStatus
from app.modules.ai.agents.hitl.events import _ui_to_dict
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.agents.safety.auto_disable import check_user_disabled
from app.modules.ai.schemas.confirm import ConfirmRequest, ConfirmResponse
from app.modules.ai.service.chat_run_service import (
    chat_run_finalizer,
    chat_run_guard,
)
from app.modules.ai.service.chat_service import chat_service
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.result_projection_service import (
    result_projection_service,
)
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()

_EXECUTION_LEASE_TTL = timedelta(minutes=1)
_EXECUTION_LEASE_RENEW_INTERVAL_SEC = 20


def _prepared_response(action) -> ConfirmResponse:  # noqa: ANN001
    status = action.status
    if status in {
        PreparedActionStatus.APPROVED.value,
        PreparedActionStatus.RUNNING.value,
    }:
        status = "running"
    return ConfirmResponse(
        actionId=action.action_id,
        toolCallId=action.execute_tool_call_id,
        status=status,
    )


def _is_expired(value: datetime) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value <= datetime.now(UTC)


async def _keep_execution_lease_alive(
    action_id: int,
    execution_owner: str,
) -> None:
    """Renew the durable RUNNING lease while this worker owns execution."""
    while True:
        await asyncio.sleep(_EXECUTION_LEASE_RENEW_INTERVAL_SEC)
        try:
            async with AsyncSessionLocal() as lease_db:
                async with lease_db.begin():
                    renewed = await prepared_action_service.renew_execution_lease(
                        lease_db,
                        action_id=action_id,
                        execution_owner=execution_owner,
                        lease_expires_at=datetime.now(UTC) + _EXECUTION_LEASE_TTL,
                    )
            if not renewed:
                return
        except Exception:
            logger.warning(
                "prepared action execution lease renewal failed action_id=%s",
                action_id,
                exc_info=True,
            )


async def _notify_prepared_terminal(action, decision: ConfirmAction) -> None:  # noqa: ANN001
    """Notify an online waiter; only clean guard/cache when no stream owns them."""
    waiter_woken = False
    try:
        waiter_woken = await hitl_manager.wake(action.confirmation_id, decision)
    except Exception:
        logger.info(
            "prepared terminal waiter notification unavailable confirmation_id=%s",
            action.confirmation_id,
            exc_info=True,
        )
    if waiter_woken:
        # The live SSE still needs the same owner lease while it reads the durable
        # result, resumes the model, commits the final assistant projection, and
        # then performs compare-owner release.  It also owns pending cleanup so
        # redis_pubsub mode retains wake_action until the waiter observes it.
        return
    if action.guard_owner_token:
        try:
            await chat_run_guard.release(
                redis_client,
                conversation_id=action.conversation_id,
                owner_token=action.guard_owner_token,
            )
        except Exception:
            logger.info(
                "prepared terminal guard cleanup unavailable action_id=%s",
                action.action_id,
                exc_info=True,
            )
    try:
        await hitl_manager.delete_pending(redis_client, action.confirmation_id)
    except Exception:
        logger.info(
            "prepared terminal cache cleanup unavailable action_id=%s",
            action.action_id,
            exc_info=True,
        )


async def _terminalize_legacy_execution_denied(
    db: AsyncSession,
    *,
    confirmation_id: str,
    pending,
    current_user: User,
    error_code: str,
    error_msg: str,
) -> None:  # noqa: ANN001
    """收口 rolling-upgrade 遗留的 Redis-only approve，不执行 Tool。"""
    log = await operation_log_service.get_by_tool_call_id(
        db,
        pending.tool_call_id,
        user_id=current_user.user_id,
        tenant_id=pending.tenant_id,
    )
    if log is not None:
        transitioned = await operation_log_service.mark_expired_if_pending(
            db, log.log_id
        )
        if transitioned is not None:
            await chat_run_finalizer.finalize_pending_turn(
                db,
                pending=pending,
                ok=False,
                error_code=error_code,
                error_msg=error_msg,
            )
        await db.commit()

    waiter_woken = await hitl_manager.wake(confirmation_id, ConfirmAction.REJECTED)
    if waiter_woken:
        return
    if pending.guard_owner_token:
        await chat_run_guard.release(
            redis_client,
            conversation_id=pending.conversation_id,
            owner_token=pending.guard_owner_token,
        )
    await hitl_manager.delete_pending(redis_client, confirmation_id)


async def _terminalize_before_execution(
    db: AsyncSession,
    *,
    action,
    log,
    status: PreparedActionStatus,
    error_code: str,
    error_msg: str,
    approved_by: int | None = None,
):  # noqa: ANN001
    terminal = await prepared_action_service.transition_status(
        db,
        action_id=action.action_id,
        expected_status=PreparedActionStatus.PENDING_CONFIRMATION,
        expected_version=action.row_version,
        target_status=status,
        approved_by=approved_by,
        error_code=error_code,
    )
    if terminal is None:
        return action
    if log is not None:
        if status == PreparedActionStatus.REJECTED:
            await operation_log_service.mark_rejected_if_pending(
                db, log.log_id, approved_by=approved_by or action.user_id
            )
        else:
            await operation_log_service.mark_expired_if_pending(db, log.log_id)
    await chat_run_finalizer.finalize_prepared_action(
        db,
        action=terminal,
        ok=False,
        error_code=error_code,
        error_msg=error_msg,
    )
    return terminal


async def _confirm_prepared(
    req: ConfirmRequest,
    *,
    db: AsyncSession,
    current_user: User,
    current_tenant_id: int,
    action_ref,
) -> ResponseModel[ConfirmResponse]:  # noqa: ANN001
    """PostgreSQL-authoritative prepared confirmation and inline execution."""
    if (
        action_ref.user_id != current_user.user_id
        or action_ref.tenant_id != current_tenant_id
    ):
        raise NotFoundException(
            "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
        )
    context = await prepared_action_service.lock_confirmation_context(
        db, confirmation_id=req.confirmation_id
    )
    if context is None:
        raise NotFoundException(
            "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
        )
    action = context.action
    if (
        action.user_id != current_user.user_id
        or action.tenant_id != current_tenant_id
        or context.conversation.user_id != current_user.user_id
    ):
        raise NotFoundException(
            "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
        )
    status = PreparedActionStatus(action.status)
    if status != PreparedActionStatus.PENDING_CONFIRMATION:
        if req.action == "approve":
            ensure_ai_chat_use(current_user)
            if await check_user_disabled(redis_client, current_user.user_id):
                raise AuthorizationException(
                    "AI 已被禁用，无法确认操作",
                    error_code="AI_USER_DISABLED",
                )
        return ResponseModel.success(data=_prepared_response(action))

    log = await operation_log_service.get_by_tool_call_id(
        db,
        action.execute_tool_call_id,
        user_id=current_user.user_id,
        tenant_id=current_tenant_id,
    )
    source_active = (
        context.source_message.conversation_id == action.conversation_id
        and context.source_message.role == "user"
        and context.source_message.is_active
    )
    if _is_expired(action.expires_at) or not source_active:
        error_code = (
            "AI_HITL_EXPIRED"
            if _is_expired(action.expires_at)
            else "AI_PREPARED_ACTION_SOURCE_STALE"
        )
        terminal = await _terminalize_before_execution(
            db,
            action=action,
            log=log,
            status=PreparedActionStatus.EXPIRED,
            error_code=error_code,
            error_msg="确认已过期，请重新发起预览",
        )
        await db.commit()
        await _notify_prepared_terminal(terminal, ConfirmAction.REJECTED)
        return ResponseModel.success(data=_prepared_response(terminal))

    if req.action == "reject":
        terminal = await _terminalize_before_execution(
            db,
            action=action,
            log=log,
            status=PreparedActionStatus.REJECTED,
            error_code="USER_REJECTED",
            error_msg="用户已取消此操作",
            approved_by=current_user.user_id,
        )
        await db.commit()
        await _notify_prepared_terminal(terminal, ConfirmAction.REJECTED)
        return ResponseModel.success(data=_prepared_response(terminal))

    try:
        ensure_ai_chat_use(current_user)
    except AuthorizationException:
        terminal = await _terminalize_before_execution(
            db,
            action=action,
            log=log,
            status=PreparedActionStatus.EXPIRED,
            error_code="AI_CHAT_PERMISSION_DENIED",
            error_msg="AI 入口权限已撤销，操作未执行",
        )
        await db.commit()
        await _notify_prepared_terminal(terminal, ConfirmAction.REJECTED)
        raise

    if await check_user_disabled(redis_client, current_user.user_id):
        terminal = await _terminalize_before_execution(
            db,
            action=action,
            log=log,
            status=PreparedActionStatus.EXPIRED,
            error_code="AI_USER_DISABLED",
            error_msg="AI 已被禁用，操作未执行",
        )
        await db.commit()
        await _notify_prepared_terminal(terminal, ConfirmAction.REJECTED)
        raise AuthorizationException(
            "AI 已被禁用，无法确认操作", error_code="AI_USER_DISABLED"
        )

    try:
        await prepared_action_service.validate_snapshot(db, action)
        deps = await chat_service.build_chat_deps(
            db,
            current_user,
            agent_code=action.agent_code,
            trace_id=action.trace_id,
            conversation_id=action.conversation_id,
        )
        deps.conversation_id = action.conversation_id
        deps.source_user_message_id = action.source_user_message_id
        deps.guard_owner_token = action.guard_owner_token
        deps.command_action = action.command_action
        current_data_scope_hash = getattr(deps, "data_scope_hash", None)
        prepared_action_service.validate_data_scope_snapshot(
            action,
            current_data_scope_hash=current_data_scope_hash,
        )
        validate_prepared_execution(action, deps)
    except BusinessException as exc:
        terminal = await _terminalize_before_execution(
            db,
            action=action,
            log=log,
            status=PreparedActionStatus.EXPIRED,
            error_code=exc.error_code or "AI_PREPARED_ACTION_REVALIDATION_FAILED",
            error_msg=exc.message,
        )
        await db.commit()
        await _notify_prepared_terminal(terminal, ConfirmAction.REJECTED)
        raise

    approved = await prepared_action_service.transition_status(
        db,
        action_id=action.action_id,
        expected_status=PreparedActionStatus.PENDING_CONFIRMATION,
        expected_version=action.row_version,
        target_status=PreparedActionStatus.APPROVED,
        approved_by=current_user.user_id,
    )
    if approved is None:
        await db.rollback()
        latest = await prepared_action_service.get_by_confirmation_id(
            db, req.confirmation_id
        )
        if latest is None:
            raise NotFoundException(
                "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
            )
        return ResponseModel.success(data=_prepared_response(latest))
    execution_owner = secrets.token_urlsafe(16)
    running = await prepared_action_service.transition_status(
        db,
        action_id=approved.action_id,
        expected_status=PreparedActionStatus.APPROVED,
        expected_version=approved.row_version,
        target_status=PreparedActionStatus.RUNNING,
        execution_owner=execution_owner,
        execution_lease_expires_at=datetime.now(UTC) + _EXECUTION_LEASE_TTL,
    )
    if running is None:
        await db.rollback()
        latest = await prepared_action_service.get_by_confirmation_id(
            db, req.confirmation_id
        )
        return ResponseModel.success(data=_prepared_response(latest or approved))
    if log is not None:
        await operation_log_service.mark_approved(
            db, log.log_id, approved_by=current_user.user_id
        )
        await operation_log_service.mark_running(db, log.log_id)
    await db.commit()

    started_at = time.monotonic()
    lease_task = asyncio.create_task(
        _keep_execution_lease_alive(running.action_id, execution_owner)
    )
    try:
        try:
            result = await execute_approved_prepared_action(running, deps)
        except Exception:
            logger.exception(
                "prepared action execution failed unexpectedly action_id=%s",
                running.action_id,
            )
            result = ToolResult.failure(
                error_code="AI_INTERNAL_ERROR",
                error_msg="工具执行失败，请稍后重新发起",
            )
    finally:
        lease_task.cancel()
        with suppress(asyncio.CancelledError):
            await lease_task
    duration_ms = int((time.monotonic() - started_at) * 1000)
    target_status = (
        PreparedActionStatus.SUCCEEDED if result.ok else PreparedActionStatus.FAILED
    )
    async with AsyncSessionLocal() as terminal_db:
        async with terminal_db.begin():
            current = await prepared_action_service.get_by_confirmation_id(
                terminal_db, req.confirmation_id
            )
            if current is None:
                raise NotFoundException(
                    "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
                )
            was_running = current.status == PreparedActionStatus.RUNNING.value
            result_lineage = None
            if result.ok and result.projection is not None:
                result_lineage = result_projection_service.freeze_lineage(
                    tenant_id=current.tenant_id,
                    agent_code=current.agent_code,
                    tool_codes=current.tool_codes or [current.execute_tool_name],
                    subject_refs=result.projection.subject_refs,
                    data_scope_hash=(
                        current_data_scope_hash
                        if result.projection.scope_bound
                        else None
                    ),
                )
            terminal = await prepared_action_service.transition_status(
                terminal_db,
                action_id=current.action_id,
                expected_status=PreparedActionStatus.RUNNING,
                expected_version=current.row_version,
                target_status=target_status,
                error_code=result.error_code or None,
                result_data=result.data if result.ok else None,
                result_ui=_ui_to_dict(result.ui) if result.ok else None,
                duration_ms=duration_ms,
                result_lineage=result_lineage,
                replace_result_lineage=result.ok,
            )
            if terminal is None:
                terminal = current
            terminal_log = await operation_log_service.get_by_tool_call_id(
                terminal_db,
                terminal.execute_tool_call_id,
                user_id=current_user.user_id,
                tenant_id=current_tenant_id,
            )
            if terminal_log is not None and was_running:
                if result.ok:
                    await operation_log_service.mark_success(
                        terminal_db,
                        terminal_log.log_id,
                        result_summary="prepared action succeeded",
                        duration_ms=duration_ms,
                    )
                else:
                    await operation_log_service.mark_failed(
                        terminal_db,
                        terminal_log.log_id,
                        error_code=result.error_code or "AI_INTERNAL_ERROR",
                        duration_ms=duration_ms,
                    )
            await chat_run_finalizer.finalize_prepared_action(
                terminal_db,
                action=terminal,
                ok=result.ok,
                duration_ms=duration_ms,
                result=result.data if result.ok else None,
                result_ui=_ui_to_dict(result.ui) if result.ok else None,
                error_code=result.error_code or None,
                error_msg=result.error_msg or None,
            )

    await _notify_prepared_terminal(terminal, ConfirmAction.APPROVED)
    return ResponseModel.success(data=_prepared_response(terminal))


@router.post("", summary="HITL 工具调用确认")
async def confirm_tool(
    req: ConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ResponseModel[ConfirmResponse]:
    """用户在 HITL 抽屉点确认 / 取消

    确认流程：
      - confirmation_id 不可枚举（secrets.token_urlsafe(32)）
      - 5min TTL，过期自动 reject
      - 必须原会话所有者确认
      - 修订 S-13：必须查 check_user_disabled
      - 修订 S-14：wake 失败时返回 status="stream_gone"
    """
    current_tenant_id = resolve_tenant_id(current_user)
    prepared_action = await prepared_action_service.get_by_confirmation_id(
        db, req.confirmation_id
    )
    if prepared_action is not None:
        return await _confirm_prepared(
            req,
            db=db,
            current_user=current_user,
            current_tenant_id=current_tenant_id,
            action_ref=prepared_action,
        )

    # 1. legacy direct HITL 继续从 Redis pending 恢复
    pending = await hitl_manager.get_pending(redis_client, req.confirmation_id)
    if pending is None:
        # 不存在 / 已过期 / 服务重启清扫后
        raise NotFoundException(
            "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
        )

    # 2. owner 校验
    if pending.user_id != current_user.user_id:
        # 禁止用户确认不属于自己会话的操作。
        logger.warning(
            "HITL confirm owner mismatch: confirmation_id=%s pending_user=%d current_user=%d",
            req.confirmation_id,
            pending.user_id,
            current_user.user_id,
        )
        raise AuthorizationException(error_code="NOT_CONFIRMATION_OWNER")
    if pending.tenant_id != current_tenant_id:
        logger.warning(
            "HITL confirm tenant mismatch: confirmation_id=%s "
            "pending_tenant=%d current_tenant=%d",
            req.confirmation_id,
            pending.tenant_id,
            current_tenant_id,
        )
        # 与 owner mismatch 共用拒绝语义，避免泄露其它租户的 pending。
        raise AuthorizationException(error_code="NOT_CONFIRMATION_OWNER")

    # 3. approve 才复验执行入口与禁用状态；reject 仅按 owner + tenant 收口。
    if req.action == "approve":
        try:
            ensure_ai_chat_use(current_user)
        except AuthorizationException:
            await _terminalize_legacy_execution_denied(
                db,
                confirmation_id=req.confirmation_id,
                pending=pending,
                current_user=current_user,
                error_code="AI_CHAT_PERMISSION_DENIED",
                error_msg="AI 入口权限已撤销，操作未执行",
            )
            raise
        if await check_user_disabled(redis_client, current_user.user_id):
            logger.warning(
                "HITL confirm blocked: user auto-disabled "
                "confirmation_id=%s user_id=%d",
                req.confirmation_id,
                current_user.user_id,
            )
            await _terminalize_legacy_execution_denied(
                db,
                confirmation_id=req.confirmation_id,
                pending=pending,
                current_user=current_user,
                error_code="AI_USER_DISABLED",
                error_msg="AI 已被禁用，操作未执行",
            )
            raise AuthorizationException(
                "AI 已被禁用，无法确认操作",
                error_code="AI_USER_DISABLED",
            )

    # 4. 写 ai_operation_log.approved_by（审计追责，无论 stream 是否还在）
    # 拒绝操作同样记录 approved_by，表示执行确认动作的用户。
    log = await operation_log_service.get_by_tool_call_id(
        db,
        pending.tool_call_id,
        user_id=current_user.user_id,
        tenant_id=current_tenant_id,
    )
    if log is not None:
        await operation_log_service.mark_approved(
            db, log.log_id, approved_by=current_user.user_id
        )
        await db.commit()

    # 5. 唤醒挂起的 SSE 流
    action = (
        ConfirmAction.APPROVED if req.action == "approve" else ConfirmAction.REJECTED
    )
    woken = await hitl_manager.wake(req.confirmation_id, action)

    if not woken:
        # 修订 S-14：wake 返回 False = 流已断（服务重启 / 单 worker 切换 /
        # SSE 已被中断 / 双击 race）。tool 不会执行，必须：
        #   a) mark_expired_if_pending 兜底审计（仅 pending_confirmation 状态迁移）
        #   b) 返回 status="stream_gone"（前端立即停止轮询，提示用户重新发起）
        logger.info(
            "HITL confirm: stream already gone confirmation_id=%s tool_call_id=%s",
            req.confirmation_id,
            pending.tool_call_id,
        )
        # 独立 session 写 operation + assistant projection；事务 commit 后才能释放
        # conversation guard。即使原 SSE 已不存在，reload 也能看到终态卡片。
        try:
            terminalized = False
            async with AsyncSessionLocal() as cleanup_db:
                async with cleanup_db.begin():
                    if log is not None:
                        if action == ConfirmAction.REJECTED:
                            transitioned = (
                                await operation_log_service.mark_rejected_if_pending(
                                    cleanup_db,
                                    log.log_id,
                                    approved_by=current_user.user_id,
                                )
                            )
                        else:
                            transitioned = (
                                await operation_log_service.mark_expired_if_pending(
                                    cleanup_db, log.log_id
                                )
                            )
                        terminalized = transitioned is not None
                    if terminalized:
                        await chat_run_finalizer.finalize_pending_turn(
                            cleanup_db,
                            pending=pending,
                            ok=False,
                            error_code=(
                                "USER_REJECTED"
                                if action == ConfirmAction.REJECTED
                                else "AI_HITL_STREAM_GONE"
                            ),
                            error_msg=(
                                "用户已取消此操作"
                                if action == ConfirmAction.REJECTED
                                else "原对话流已断开，请重新发起"
                            ),
                        )
            if terminalized:
                if pending.guard_owner_token:
                    await chat_run_guard.release(
                        redis_client,
                        conversation_id=pending.conversation_id,
                        owner_token=pending.guard_owner_token,
                    )
                await hitl_manager.delete_pending(redis_client, req.confirmation_id)
        except Exception:
            # terminal commit 失败不释放 guard；lease 仅作为最后防死锁兜底。
            logger.exception(
                "offline terminal finalization failed confirmation_id=%s log_id=%s",
                req.confirmation_id,
                log.log_id if log else None,
            )

        return ResponseModel.success(
            data=ConfirmResponse(
                toolCallId=pending.tool_call_id,
                status="stream_gone",
            )
        )

    # 6. 唤醒成功，stream 会自己处理 approve/reject
    return ResponseModel.success(
        data=ConfirmResponse(
            toolCallId=pending.tool_call_id,
            status="queued",
        )
    )


# 给 lint / IDE：避免未使用导入告警，执行器会使用 BusinessRuleException。
_ = BusinessRuleException
