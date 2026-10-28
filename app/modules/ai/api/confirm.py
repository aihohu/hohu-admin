"""HITL approval with durable prepared-action completion.

POST /ai/confirm authenticates the owner and current authority before claiming
the action and required audit. The Gateway owns the business transaction;
subsequent chat projection reads validated completion facts in a fresh session.
Uncertain commits remain running and never authorize replay. The Redis-only
legacy confirmation path retains its separate compatibility behavior.
"""

import asyncio
import logging
import secrets
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
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
from app.core.tenant import TenantContext, get_bound_tenant_context
from app.db.authorization_http import get_authorization_runtime, release_request_reads
from app.db.authorization_lifecycle_owner import PendingConfirmationRevalidationFailed
from app.db.request_session import RequestSession
from app.db.session import AsyncSessionLocal, get_db
from app.modules.ai.agents.gateway.atomic_execution import (
    OUTCOME_UNKNOWN_MESSAGE,
    PreparedExecutionOutcomeUnknown,
    _commit_with_budget,
    _discard_uncertain_session,
)
from app.modules.ai.agents.gateway.executor import (
    execute_approved_prepared_action,
    record_prepared_execution_outcome,
    validate_prepared_execution,
)
from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.agents.hitl.constants import (
    AiOperationStatus,
    ConfirmAction,
    PreparedActionStatus,
)
from app.modules.ai.agents.hitl.events import _ui_to_dict
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.agents.safety.auto_disable import check_user_disabled
from app.modules.ai.schemas.confirm import ConfirmRequest, ConfirmResponse
from app.modules.ai.service.atomic_completion_service import (
    DB_TOOLS,
    FILE_TOOLS,
    ClaimBinding,
    atomic_completion_service,
    has_protocol_marker,
    is_registered_readonly_action,
    new_execution_owner,
)
from app.modules.ai.service.authorization_execution_candidate import (
    ExecutionDisposition,
)
from app.modules.ai.service.chat_run_service import (
    chat_run_finalizer,
    chat_run_guard,
)
from app.modules.ai.service.chat_service import chat_service
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.ai.service.reconciliation_annotation import has_annotation
from app.modules.ai.service.result_projection_service import (
    result_projection_service,
)
from app.modules.auth.service import access_identity, get_current_user
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


def _unknown_response(claim: ClaimBinding) -> ResponseModel[ConfirmResponse]:
    return ResponseModel.success(
        msg=OUTCOME_UNKNOWN_MESSAGE,
        data=ConfirmResponse(
            actionId=claim.action_id,
            toolCallId=claim.execute_tool_call_id,
            status="running",
        ),
    )


async def _read_unexecuted_response(action_ref, *, tenant: TenantContext):  # noqa: ANN001
    """A cancelled label is terminal only when its never-started proof binds."""
    try:
        action = await get_authorization_runtime().lifecycle.unexecuted(
            action_ref.confirmation_id, tenant=tenant
        )
        return ResponseModel.success(data=_prepared_response(action))
    except (Exception, asyncio.CancelledError):
        return _unknown_response(action_ref)


async def reconcile_prepared_completion(
    confirmation_id, *, tenant, expected_claim=None, allow_fence=False
):
    return await get_authorization_runtime().recovery.reconcile(
        confirmation_id,
        tenant=tenant,
        expected_claim=expected_claim,
        allow_fence=allow_fence,
    )


async def _project_prepared_completion(
    confirmation_id: str,
    *,
    current_user_id: int,
    tenant: TenantContext,
    action_ref=None,
    known_outcome: ExecutionDisposition | None = None,
) -> ResponseModel[ConfirmResponse]:
    """Read verified facts, then independently repair the current projection."""
    if current_user_id != tenant.actor_user_id:
        raise NotFoundException(
            "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
        )
    runtime = get_authorization_runtime()
    outcome = await runtime.recovery.reconcile(
        confirmation_id,
        tenant=tenant,
        expected_claim=action_ref if isinstance(action_ref, ClaimBinding) else None,
    )
    if outcome.disposition not in {"succeeded", "failed"} or outcome.action is None:
        if (
            type(known_outcome) is ExecutionDisposition
            and known_outcome.disposition in {"succeeded", "failed"}
            and type(action_ref) is ClaimBinding
            and action_ref.tenant_id == tenant.tenant_id
            and action_ref.actor_user_id == current_user_id
        ):
            return ResponseModel.success(
                msg="操作结果已保存，显示同步暂未完成，请刷新会话。",
                data=ConfirmResponse(
                    actionId=action_ref.action_id,
                    toolCallId=action_ref.execute_tool_call_id,
                    status=known_outcome.disposition,
                ),
            )
        if action_ref is not None:
            return _unknown_response(action_ref)
        raise NotFoundException(
            "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
        )
    terminal = outcome.action
    response = _prepared_response(terminal)
    display_failed = False
    try:
        async with asyncio.timeout(1):
            await runtime.chat.recover(tenant=tenant, confirmation_id=confirmation_id)
    except Exception:
        # This transaction can only repair display facts. Its ACK uncertainty
        # neither erases a business terminal nor permits executing it again.
        display_failed = True
        logger.warning(
            "Prepared completion display unavailable confirmation_id=%s",
            confirmation_id,
        )
    with suppress(Exception):
        async with asyncio.timeout(0.25):
            await _notify_prepared_terminal(
                terminal, ConfirmAction.APPROVED, tenant=tenant
            )
    if display_failed:
        return ResponseModel.success(
            msg="操作结果已保存，显示同步暂未完成，请刷新会话。", data=response
        )
    return ResponseModel.success(data=response)


def _is_expired(value: datetime) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value <= datetime.now(UTC)


async def _keep_execution_lease_alive(
    action_id: int,
    execution_owner: str,
    tenant: TenantContext,
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
                        tenant=tenant,
                    )
            if not renewed:
                return
        except Exception:
            logger.warning(
                "prepared action execution lease renewal failed action_id=%s",
                action_id,
                exc_info=True,
            )


async def _notify_prepared_terminal(
    action, decision: ConfirmAction, *, tenant: TenantContext
) -> None:  # noqa: ANN001
    """Notify an online waiter; only clean guard/cache when no stream owns them."""
    waiter_woken = False
    try:
        waiter_woken = await hitl_manager.wake(
            action.confirmation_id, decision, tenant=tenant
        )
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
                tenant=tenant,
            )
        except Exception:
            logger.info(
                "prepared terminal guard cleanup unavailable action_id=%s",
                action.action_id,
                exc_info=True,
            )
    try:
        await hitl_manager.delete_pending(
            redis_client, action.confirmation_id, tenant=tenant
        )
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
    tenant: TenantContext,
) -> None:  # noqa: ANN001
    """收口 rolling-upgrade 遗留的 Redis-only approve，不执行 Tool。"""
    log = await operation_log_service.get_by_tool_call_id(
        db,
        pending.tool_call_id,
        user_id=current_user.user_id,
        tenant=tenant,
    )
    if log is not None:
        transitioned = await operation_log_service.mark_expired_if_pending(
            db,
            log.log_id,
            error_code=error_code,
            tenant=tenant,
        )
        if transitioned is not None:
            await chat_run_finalizer.finalize_pending_turn(
                db,
                pending=pending,
                ok=False,
                error_code=error_code,
                error_msg=error_msg,
                tenant=tenant,
            )
        await db.commit()

    waiter_woken = await hitl_manager.wake(
        confirmation_id, ConfirmAction.REJECTED, tenant=tenant
    )
    if waiter_woken:
        return
    if pending.guard_owner_token:
        await chat_run_guard.release(
            redis_client,
            conversation_id=pending.conversation_id,
            owner_token=pending.guard_owner_token,
            tenant=tenant,
        )
    await hitl_manager.delete_pending(redis_client, confirmation_id, tenant=tenant)


async def _terminalize_before_execution(
    db: AsyncSession,
    *,
    action,
    log,
    status: PreparedActionStatus,
    error_code: str,
    error_msg: str,
    approved_by: int | None = None,
    tenant: TenantContext,
):  # noqa: ANN001
    terminal = await prepared_action_service.transition_status(
        db,
        action_id=action.action_id,
        expected_status=PreparedActionStatus.PENDING_CONFIRMATION,
        expected_version=action.row_version,
        target_status=status,
        approved_by=approved_by,
        error_code=error_code,
        tenant=tenant,
    )
    if terminal is None:
        return action
    if log is not None:
        if status == PreparedActionStatus.REJECTED:
            await operation_log_service.mark_rejected_if_pending(
                db,
                log.log_id,
                approved_by=approved_by or action.user_id,
                tenant=tenant,
            )
        else:
            await operation_log_service.mark_expired_if_pending(
                db,
                log.log_id,
                error_code=error_code,
                tenant=tenant,
            )
    await chat_run_finalizer.finalize_prepared_action(
        db,
        action=terminal,
        ok=False,
        error_code=error_code,
        error_msg=error_msg,
        tenant=tenant,
    )
    return terminal


async def _repair_cancelled_display(action, *, tenant):
    with suppress(Exception):
        async with asyncio.timeout(1):
            await get_authorization_runtime().chat.recover(
                tenant=tenant, confirmation_id=action.confirmation_id
            )
    with suppress(Exception):
        async with asyncio.timeout(0.25):
            await _notify_prepared_terminal(
                action, ConfirmAction.REJECTED, tenant=tenant
            )


async def _confirm_planned(req, *, db, current_user, action_ref):
    identity = access_identity(current_user)
    if action_ref.user_id != identity.tenant.actor_user_id:
        raise NotFoundException(
            "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
        )
    # Internal callers use the same path; only the request class needs its
    # permanent handoff marker. Neither branch may carry pending writes.
    if isinstance(db, RequestSession):
        await release_request_reads(db)
    else:
        if db.new or db.dirty or db.deleted:
            raise BusinessRuleException(
                "Confirmation session has pending writes",
                error_code="AUTHORIZATION_LOCK_ORDER_INVALID",
            )
        db.expunge_all()
        await db.rollback()
    disabled = False
    if req.action == "approve" and action_ref.status == "pending_confirmation":
        async with asyncio.timeout(2):
            disabled = await check_user_disabled(
                redis_client, identity.tenant.actor_user_id, tenant=identity.tenant
            )
    runtime = get_authorization_runtime()
    try:
        admission = await runtime.lifecycle.admit(
            req.confirmation_id,
            tenant=identity.tenant,
            actor_user_version=identity.user_version,
            decision=req.action,
            disabled=disabled,
        )
    except BusinessException as exc:
        if exc.error_code in {
            "AUTHORIZATION_COMMIT_UNKNOWN",
            "AUTHORIZATION_TRANSACTION_TIMEOUT",
            "FIELD_POLICY_UNAVAILABLE",
        }:
            return _unknown_response(action_ref)
        raise
    if admission.disposition == "unknown":
        return _unknown_response(action_ref)
    if admission.disposition in {"rejected", "expired"}:
        if not admission.changed:
            return await _read_unexecuted_response(
                admission.action, tenant=identity.tenant
            )
        await _repair_cancelled_display(admission.action, tenant=identity.tenant)
        if admission.error is not None:
            raise admission.error
        return ResponseModel.success(data=_prepared_response(admission.action))
    known_outcome = None
    if admission.disposition == "pending_confirmation":
        try:
            outcome = await runtime.lifecycle.confirm(
                req.confirmation_id,
                tenant=identity.tenant,
                actor_user_version=identity.user_version,
            )
        except PendingConfirmationRevalidationFailed as exc:
            try:
                denied = await runtime.lifecycle.admit(
                    req.confirmation_id,
                    tenant=identity.tenant,
                    actor_user_version=identity.user_version,
                    decision="approve",
                    disabled=disabled,
                    denial=exc.denial,
                )
            except BusinessException as denial_error:
                if denial_error.error_code in {
                    "AUTHORIZATION_COMMIT_UNKNOWN",
                    "AUTHORIZATION_TRANSACTION_TIMEOUT",
                    "FIELD_POLICY_UNAVAILABLE",
                }:
                    return _unknown_response(action_ref)
                raise
            if not denied.changed:
                return _unknown_response(action_ref)
            await _repair_cancelled_display(denied.action, tenant=identity.tenant)
            raise exc.denial
        except BusinessException as exc:
            if exc.error_code in {
                "AUTHORIZATION_COMMIT_UNKNOWN",
                "AUTHORIZATION_TRANSACTION_TIMEOUT",
                "FIELD_POLICY_UNAVAILABLE",
            }:
                return _unknown_response(action_ref)
            raise
        if outcome.disposition == "unknown":
            return _unknown_response(action_ref)
        known_outcome = outcome
        if outcome.claim is not None:
            await record_prepared_execution_outcome(
                outcome.claim, outcome, tenant=identity.tenant
            )
    return await _project_prepared_completion(
        req.confirmation_id,
        current_user_id=identity.tenant.actor_user_id,
        tenant=identity.tenant,
        action_ref=known_outcome.claim
        if known_outcome is not None and known_outcome.claim is not None
        else admission.action,
        known_outcome=known_outcome,
    )


async def _confirm_prepared(
    req: ConfirmRequest,
    *,
    db: AsyncSession,
    current_user: User,
    tenant: TenantContext,
    action_ref,
) -> ResponseModel[ConfirmResponse]:  # noqa: ANN001
    """PostgreSQL-authoritative prepared confirmation and inline execution."""
    current_user_id = int(current_user.user_id)
    if (
        action_ref.user_id != current_user_id
        or action_ref.tenant_id != tenant.tenant_id
    ):
        raise NotFoundException(
            "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
        )
    readonly = is_registered_readonly_action(action_ref)
    annotated = await operation_log_service.has_reconciliation_annotation(
        db, action_ref.execute_tool_call_id, user_id=current_user_id, tenant=tenant
    )
    if annotated:
        return _unknown_response(action_ref)
    if not readonly and action_ref.status in {"rejected", "expired"}:
        db.expunge_all()
        await db.rollback()
        return await _read_unexecuted_response(action_ref, tenant=tenant)
    if not readonly and action_ref.status in {
        "approved",
        "running",
        "succeeded",
        "failed",
    }:
        # An execution result survives soft deletion/inactivation. Reconcile
        # existing facts without granting a second execution or requiring the
        # live-source admission check used only by a pending confirmation.
        db.expunge_all()
        await db.rollback()
        outcome = await reconcile_prepared_completion(
            req.confirmation_id, tenant=tenant
        )
        if outcome.disposition == "unknown":
            return _unknown_response(action_ref)
        return await _project_prepared_completion(
            req.confirmation_id,
            current_user_id=current_user_id,
            tenant=tenant,
            action_ref=outcome.action,
        )
    try:
        async with asyncio.timeout(8):
            await db.execute(select(func.set_config("lock_timeout", "2s", True)))
            context = await prepared_action_service.lock_confirmation_context(
                db, confirmation_id=req.confirmation_id, tenant=tenant
            )
    except (TimeoutError, DBAPIError):
        response = _unknown_response(action_ref)
        await _discard_uncertain_session(db)
        return response
    if context is None:
        # A concurrent confirmation may have started after our initial read and
        # released the live context before this lock attempt finished.
        if not is_registered_readonly_action(action_ref) and action_ref.status in {
            "approved",
            "running",
            "succeeded",
            "failed",
        }:
            db.expunge_all()
            await db.rollback()
            outcome = await reconcile_prepared_completion(
                req.confirmation_id, tenant=tenant
            )
            if outcome.disposition == "unknown":
                return _unknown_response(action_ref)
            return await _project_prepared_completion(
                req.confirmation_id,
                current_user_id=current_user_id,
                tenant=tenant,
                action_ref=outcome.action,
            )
        raise NotFoundException(
            "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
        )
    action = context.action
    if (
        action.user_id != current_user_id
        or action.tenant_id != tenant.tenant_id
        or context.conversation.user_id != current_user_id
    ):
        raise NotFoundException(
            "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
        )
    status = PreparedActionStatus(action.status)
    if status != PreparedActionStatus.PENDING_CONFIRMATION:
        if not is_registered_readonly_action(action) and status in {
            PreparedActionStatus.REJECTED,
            PreparedActionStatus.EXPIRED,
        }:
            db.expunge_all()
            await db.rollback()
            return await _read_unexecuted_response(action, tenant=tenant)
        if status == PreparedActionStatus.RUNNING and has_protocol_marker(
            action.execution_owner
        ):
            db.expunge_all()
            await db.rollback()
            outcome = await reconcile_prepared_completion(
                req.confirmation_id, tenant=tenant
            )
            if outcome.disposition == "unknown":
                return _unknown_response(action)
            return await _project_prepared_completion(
                req.confirmation_id,
                current_user_id=current_user_id,
                tenant=tenant,
                action_ref=outcome.action,
            )
        if req.action == "approve":
            ensure_ai_chat_use(current_user)
            if await check_user_disabled(redis_client, current_user_id, tenant=tenant):
                raise AuthorizationException(
                    "AI 已被禁用，无法确认操作",
                    error_code="AI_USER_DISABLED",
                )
        if (
            status == PreparedActionStatus.SUCCEEDED
            and action.execute_tool_name in DB_TOOLS | FILE_TOOLS
        ) or (
            status == PreparedActionStatus.FAILED
            and has_protocol_marker(action.execution_owner)
        ):
            db.expunge_all()
            await db.rollback()
            return await _project_prepared_completion(
                req.confirmation_id,
                current_user_id=current_user_id,
                tenant=tenant,
                action_ref=action,
            )
        return ResponseModel.success(data=_prepared_response(action))

    log = await operation_log_service.get_by_tool_call_id(
        db,
        action.execute_tool_call_id,
        user_id=current_user_id,
        tenant=tenant,
    )
    source_active = (
        context.source_message.conversation_id == action.conversation_id
        and context.source_message.role == "user"
        and context.source_message.is_active
    )
    if log is not None and has_annotation(getattr(log, "result_summary", None)):
        return _unknown_response(action)
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
            tenant=tenant,
        )
        await db.commit()
        await _notify_prepared_terminal(terminal, ConfirmAction.REJECTED, tenant=tenant)
        return ResponseModel.success(data=_prepared_response(terminal))

    if req.action == "reject":
        terminal = await _terminalize_before_execution(
            db,
            action=action,
            log=log,
            status=PreparedActionStatus.REJECTED,
            error_code="USER_REJECTED",
            error_msg="用户已取消此操作",
            approved_by=current_user_id,
            tenant=tenant,
        )
        await db.commit()
        await _notify_prepared_terminal(terminal, ConfirmAction.REJECTED, tenant=tenant)
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
            tenant=tenant,
        )
        await db.commit()
        await _notify_prepared_terminal(terminal, ConfirmAction.REJECTED, tenant=tenant)
        raise

    if await check_user_disabled(redis_client, current_user_id, tenant=tenant):
        terminal = await _terminalize_before_execution(
            db,
            action=action,
            log=log,
            status=PreparedActionStatus.EXPIRED,
            error_code="AI_USER_DISABLED",
            error_msg="AI 已被禁用，操作未执行",
            tenant=tenant,
        )
        await db.commit()
        await _notify_prepared_terminal(terminal, ConfirmAction.REJECTED, tenant=tenant)
        raise AuthorizationException(
            "AI 已被禁用，无法确认操作", error_code="AI_USER_DISABLED"
        )

    try:
        await prepared_action_service.validate_snapshot(
            db,
            action,
            tenant=get_bound_tenant_context(current_user),
        )
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
        registered = validate_prepared_execution(action, deps)
    except BusinessException as exc:
        terminal = await _terminalize_before_execution(
            db,
            action=action,
            log=log,
            status=PreparedActionStatus.EXPIRED,
            error_code=exc.error_code or "AI_PREPARED_ACTION_REVALIDATION_FAILED",
            error_msg=exc.message,
            tenant=tenant,
        )
        await db.commit()
        await _notify_prepared_terminal(terminal, ConfirmAction.REJECTED, tenant=tenant)
        raise

    atomic = not registered.meta.readonly
    if atomic:
        log = await atomic_completion_service.lock_action_log(
            db,
            action,
            tenant=tenant,
            expected_status=AiOperationStatus.PENDING_CONFIRMATION,
        )
    approved = await prepared_action_service.transition_status(
        db,
        action_id=action.action_id,
        expected_status=PreparedActionStatus.PENDING_CONFIRMATION,
        expected_version=action.row_version,
        target_status=PreparedActionStatus.APPROVED,
        approved_by=current_user_id,
        tenant=tenant,
    )
    if approved is None:
        await db.rollback()
        latest = await prepared_action_service.get_by_confirmation_id(
            db, req.confirmation_id, tenant=tenant
        )
        if latest is None:
            raise NotFoundException(
                "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
            )
        return ResponseModel.success(data=_prepared_response(latest))
    execution_owner = (
        new_execution_owner(action.execute_tool_name)
        if atomic
        else secrets.token_urlsafe(16)
    )
    running = await prepared_action_service.transition_status(
        db,
        action_id=approved.action_id,
        expected_status=PreparedActionStatus.APPROVED,
        expected_version=approved.row_version,
        target_status=PreparedActionStatus.RUNNING,
        execution_owner=execution_owner,
        execution_lease_expires_at=datetime.now(UTC) + _EXECUTION_LEASE_TTL,
        tenant=tenant,
    )
    if running is None:
        await db.rollback()
        latest = await prepared_action_service.get_by_confirmation_id(
            db, req.confirmation_id, tenant=tenant
        )
        return ResponseModel.success(data=_prepared_response(latest or approved))
    if log is not None:
        await operation_log_service.mark_approved(
            db, log.log_id, approved_by=current_user_id, tenant=tenant
        )
        await operation_log_service.mark_running(db, log.log_id, tenant=tenant)
    claim = ClaimBinding.from_action(running, log_id=log.log_id) if atomic else None
    try:
        await _commit_with_budget(db)
    except (Exception, asyncio.CancelledError):
        await _discard_uncertain_session(db)
        if claim is None:
            raise
        outcome = await reconcile_prepared_completion(
            req.confirmation_id,
            tenant=tenant,
            expected_claim=claim,
            allow_fence=True,
        )
        if outcome.disposition == "unknown":
            return _unknown_response(claim)
        return await _project_prepared_completion(
            req.confirmation_id,
            current_user_id=current_user_id,
            tenant=tenant,
            action_ref=claim,
        )

    if claim is not None:
        # No request-session ORM instance may later write back an old RUNNING
        # state. Business and completion now belong to the Gateway transaction.
        db.expunge_all()
        try:
            known_outcome = await execute_approved_prepared_action(claim, deps)
        except PreparedExecutionOutcomeUnknown:
            return _unknown_response(claim)
        return await _project_prepared_completion(
            req.confirmation_id,
            current_user_id=current_user_id,
            tenant=tenant,
            action_ref=claim,
            known_outcome=known_outcome,
        )

    started_at = time.monotonic()
    lease_task = asyncio.create_task(
        _keep_execution_lease_alive(running.action_id, execution_owner, tenant)
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
    if not result.ok:
        await db.rollback()
    current = await prepared_action_service.get_by_confirmation_id(
        db, req.confirmation_id, tenant=tenant
    )
    if current is None:
        raise NotFoundException(
            "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
        )
    was_running = current.status == PreparedActionStatus.RUNNING.value
    result_lineage = None
    if result.ok and result.projection is not None:
        result_data_scope_hash = None
        if result.projection.scope_bound:
            result_data_scope_hash = (
                await result_projection_service.compute_data_scope_hash(
                    db,
                    current_user,
                )
            )
        result_lineage = result_projection_service.freeze_lineage(
            tenant=tenant,
            agent_code=current.agent_code,
            tool_codes=current.tool_codes or [current.execute_tool_name],
            subject_refs=result.projection.subject_refs,
            data_scope_hash=result_data_scope_hash,
        )
    terminal = await prepared_action_service.transition_status(
        db,
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
        tenant=tenant,
    )
    if terminal is None:
        terminal = current
    terminal_log = await operation_log_service.get_by_tool_call_id(
        db,
        terminal.execute_tool_call_id,
        user_id=current_user_id,
        tenant=tenant,
    )
    if terminal_log is not None and was_running:
        if result.ok:
            await operation_log_service.mark_success(
                db,
                terminal_log.log_id,
                result_summary="prepared action succeeded",
                duration_ms=duration_ms,
                tenant=tenant,
            )
        else:
            await operation_log_service.mark_failed(
                db,
                terminal_log.log_id,
                error_code=result.error_code or "AI_INTERNAL_ERROR",
                duration_ms=duration_ms,
                tenant=tenant,
            )
    await chat_run_finalizer.finalize_prepared_action(
        db,
        action=terminal,
        ok=result.ok,
        duration_ms=duration_ms,
        result=result.data if result.ok else None,
        result_ui=_ui_to_dict(result.ui) if result.ok else None,
        error_code=result.error_code or None,
        error_msg=result.error_msg or None,
        tenant=tenant,
    )
    await db.commit()

    await _notify_prepared_terminal(terminal, ConfirmAction.APPROVED, tenant=tenant)
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
    tenant = get_bound_tenant_context(current_user)
    prepared_action = await prepared_action_service.get_by_confirmation_id(
        db, req.confirmation_id, tenant=tenant
    )
    if prepared_action is not None:
        if prepared_action.execute_tool_name in DB_TOOLS | FILE_TOOLS:
            return await _confirm_planned(
                req, db=db, current_user=current_user, action_ref=prepared_action
            )
        return await _confirm_prepared(
            req,
            db=db,
            current_user=current_user,
            tenant=tenant,
            action_ref=prepared_action,
        )

    # 1. legacy direct HITL 继续从 Redis pending 恢复
    pending = await hitl_manager.get_pending(
        redis_client, req.confirmation_id, tenant=tenant
    )
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
    if pending.tenant_id != tenant.tenant_id:
        logger.warning(
            "HITL confirm tenant mismatch: confirmation_id=%s "
            "pending_tenant=%d current_tenant=%d",
            req.confirmation_id,
            pending.tenant_id,
            tenant.tenant_id,
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
                tenant=tenant,
            )
            raise
        if await check_user_disabled(redis_client, current_user.user_id, tenant=tenant):
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
                tenant=tenant,
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
        tenant=tenant,
    )
    if log is not None:
        await operation_log_service.mark_approved(
            db, log.log_id, approved_by=current_user.user_id, tenant=tenant
        )
        await db.commit()

    # 5. 唤醒挂起的 SSE 流
    action = (
        ConfirmAction.APPROVED if req.action == "approve" else ConfirmAction.REJECTED
    )
    woken = await hitl_manager.wake(req.confirmation_id, action, tenant=tenant)

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
                                    tenant=tenant,
                                )
                            )
                        else:
                            transitioned = (
                                await operation_log_service.mark_expired_if_pending(
                                    cleanup_db, log.log_id, tenant=tenant
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
                            tenant=tenant,
                        )
            if terminalized:
                if pending.guard_owner_token:
                    await chat_run_guard.release(
                        redis_client,
                        conversation_id=pending.conversation_id,
                        owner_token=pending.guard_owner_token,
                        tenant=tenant,
                    )
                await hitl_manager.delete_pending(
                    redis_client, req.confirmation_id, tenant=tenant
                )
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
