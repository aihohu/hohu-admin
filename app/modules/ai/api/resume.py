"""SSE 流断流续传端点。

GET /ai/chat/resume
  - 读 Last-Event-ID 头作为 confirmation_id（SSE 协议标准）
  - 校验 owner / TTL
  - 获取 Redis owner 锁防止重复执行
  - emit confirmation_resumed → hang → execute_tool → emit tool_call_result + done
  - finally 释放 owner 锁（Lua 脚本防误删）

模式说明（不再硬校验）：
  - memory 模式：单 worker 本地开发可用。_hang_memory + _wake_memory 跨请求
    同进程工作，Event 在 confirm 端点被 set 后 hang 协程返回 action。
  - Redis Pub/Sub 模式支持多 worker 跨进程唤醒
    走 pubsub + SETNX owner 锁兜底。多 worker 部署用 memory 会双执行 race，
    由部署文档约束，不在端点强校验（避免本地开发被锁死）。

错误码（按出现顺序）：
  410 AI_RESUME_DISABLED         — AI_SSE_RESUME_ENABLED=False
  400 AI_RESUME_MISSING_ID       — Last-Event-ID 头 + query param 均缺
  404 AI_RESUME_NOT_FOUND        — Redis 中无 pending（已过期 / 未发起 / 重启清扫）
  403 AI_RESUME_FORBIDDEN        — 当前 user 非 pending.owner
  410 AI_RESUME_ALREADY_RESOLVED — pending.wake_action 已设（断流期间已确认/拒绝）
  422 AI_RESUME_TTL_TOO_SHORT    — TTL < 60s（执行完来不及回写结果）
  409 AI_RESUME_IN_PROGRESS      — owner 锁被其它 worker 持有（双执行兜底）
"""

import logging
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic_ai.ui import SSE_CONTENT_TYPE
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AI_CHAT_USE_PERMISSION, has_explicit_permission
from app.core.base_response import ResponseModel
from app.core.config import settings
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.redis import redis_client
from app.core.tenant import TenantContext, get_bound_tenant_context
from app.db.session import AsyncSessionLocal, get_db
from app.modules.ai.agents.gateway.executor import resume_tool_execution
from app.modules.ai.agents.gateway.result import UIResult
from app.modules.ai.agents.hitl.constants import (
    AI_HITL_OWNER_LOCK_PREFIX,
    AI_HITL_OWNER_LOCK_TTL_SEC,
    ConfirmAction,
    PreparedActionStatus,
)
from app.modules.ai.agents.hitl.events import (
    AiErrorEvent,
    ConfirmationResumedEvent,
    DoneEvent,
    DryRunSummary,
    ToolCallResultEvent,
)
from app.modules.ai.agents.hitl.manager import PendingPayload, hitl_manager
from app.modules.ai.api.chat import _format_sse_chunk
from app.modules.ai.models.message import AiMessage
from app.modules.ai.schemas.resume import ResumeStatusOut
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

# Lua 脚本：仅当 KEYS[1] 的值 == ARGV[1] 时 del（防 token 不匹配误删）
_RELEASE_LOCK_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _set_exc_code(exc: BusinessRuleException, code: int) -> BusinessRuleException:
    """将业务异常的默认 400 状态按场景调整为 409、410 或 422。"""
    exc.code = code
    return exc


def _build_resumed_event(
    confirmation_id: str,
    pending: PendingPayload,
    durable_action=None,  # noqa: ANN001
) -> ConfirmationResumedEvent:
    """从 pending payload 构造 ConfirmationResumedEvent

    pending 是 PendingPayload dataclass。注意 confirmation_id 参数从 caller 传入
    （PendingPayload 不含 confirmation_id 字段，它是 Redis key 后缀）。
    """
    dry_run: DryRunSummary | None = None
    if pending.dry_run_result:
        dry_run = DryRunSummary(
            summary=pending.dry_run_result.get("summary", ""),
            affected_count=pending.dry_run_result.get("affected_count", 0),
            summary_key=(
                pending.dry_run_result.get("summaryKey")
                or pending.dry_run_result.get("summary_key")
            ),
            summary_params=(
                pending.dry_run_result.get("summaryParams")
                or pending.dry_run_result.get("summary_params")
            ),
            affected_examples=(
                pending.dry_run_result.get("affectedExamples")
                or pending.dry_run_result.get("affected_examples")
            ),
        )
    presentation = (
        durable_action.presentation
        if durable_action is not None
        else {
            "title": pending.tool_name,
            "summary": (
                dry_run.summary if dry_run is not None else f"tool={pending.tool_name}"
            ),
            "fields": [],
            "warnings": [],
        }
    )
    return ConfirmationResumedEvent(
        confirmation_id=confirmation_id,
        tool=(
            durable_action.execute_tool_name
            if durable_action is not None
            else pending.tool_name
        ),
        tool_call_id=(
            durable_action.execute_tool_call_id
            if durable_action is not None
            else pending.tool_call_id
        ),
        summary=str(
            presentation.get("summary")
            or presentation.get("title")
            or f"resume: tool={pending.tool_name}"
        ),
        expires_at=pending.expires_at,
        resumed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        dry_run=dry_run,
        action_id=(
            durable_action.action_id
            if durable_action is not None
            else pending.action_id
        ),
        source_tool_call_id=(
            durable_action.prepare_tool_call_id if durable_action is not None else None
        ),
        interaction_flow=(
            durable_action.interaction_flow if durable_action is not None else "direct"
        ),
        presentation=presentation,
    )


def _durable_binding_valid(
    action,  # noqa: ANN001
    *,
    pending: PendingPayload,
    user_id: int,
    tenant: TenantContext,
) -> bool:
    return bool(
        action is not None
        and (pending.action_id is None or action.action_id == pending.action_id)
        and action.user_id == user_id
        and action.tenant_id == tenant.tenant_id
        and action.conversation_id == pending.conversation_id
        and action.execute_tool_call_id == pending.tool_call_id
        and action.trace_id == pending.trace_id
    )


def _pending_from_durable_action(action) -> PendingPayload:  # noqa: ANN001
    """为 Redis 已清理的 durable 终态构造只读回放上下文。"""
    expires_at = action.expires_at
    if isinstance(expires_at, datetime):
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        expires_at = expires_at.isoformat().replace("+00:00", "Z")
    return PendingPayload(
        user_id=action.user_id,
        tenant_id=action.tenant_id,
        conversation_id=action.conversation_id,
        tool_call_id=action.execute_tool_call_id,
        trace_id=action.trace_id,
        tool_name=action.execute_tool_name,
        args=action.frozen_args,
        dry_run_result=None,
        expires_at=str(expires_at),
        source_user_message_id=action.source_user_message_id,
        guard_owner_token=action.guard_owner_token,
        command_action=action.command_action,
        agent_code=action.agent_code,
        risk_level=action.risk_level,
        chip_target=action.chip_target,
        action_id=action.action_id,
    )


def _minimal_resume_status(
    confirmation_id: str,
    *,
    durable_action=None,  # noqa: ANN001
    pending: PendingPayload | None = None,
    error_code: str = "AI_CHAT_PERMISSION_DENIED",
) -> ResponseModel[ResumeStatusOut]:
    status = (
        durable_action.status
        if durable_action is not None
        else (
            pending.wake_action
            if pending is not None and pending.wake_action
            else PreparedActionStatus.PENDING_CONFIRMATION.value
        )
    )
    if status in {
        PreparedActionStatus.APPROVED.value,
        PreparedActionStatus.RUNNING.value,
    }:
        status = PreparedActionStatus.RUNNING.value
    return ResponseModel.success(
        data=ResumeStatusOut(
            confirmationId=confirmation_id,
            status=status,
            errorCode=error_code,
            finishedAt=(
                getattr(durable_action, "finished_at", None)
                if durable_action is not None
                else None
            ),
        )
    )


def _ui_result_from_dict(value: dict | None) -> UIResult | None:
    if value is None:
        return None
    return UIResult(
        view_type=str(value.get("viewType") or value.get("view_type") or "plain_json"),
        view_data=value.get("viewData") or value.get("view_data") or {},
        audit=value.get("audit") or {},
        label_key=str(value.get("labelKey") or value.get("label_key") or ""),
        label_params=value.get("labelParams") or value.get("label_params") or {},
    )


async def _load_durable_resume_terminal(
    *,
    confirmation_id: str,
    pending: PendingPayload,
    user_id: int,
    tenant: TenantContext,
    current_user: User | None = None,
) -> list[ToolCallResultEvent | AiErrorEvent | DoneEvent]:
    """Read a confirm-owned terminal fact without ever re-executing its tool."""
    try:
        async with AsyncSessionLocal() as terminal_db:
            action = await prepared_action_service.get_by_confirmation_id(
                terminal_db, confirmation_id, tenant=tenant
            )
            if not _durable_binding_valid(
                action,
                pending=pending,
                user_id=user_id,
                tenant=tenant,
            ):
                raise BusinessRuleException(
                    "续传 action 绑定无效",
                    error_code="AI_PREPARED_ACTION_BINDING_INVALID",
                )
            status = PreparedActionStatus(action.status)
            if not status.is_terminal:
                raise BusinessRuleException(
                    "续传收到唤醒但 action 尚未进入终态",
                    error_code="AI_PREPARED_ACTION_TERMINAL_MISSING",
                )
            message_id = await terminal_db.scalar(
                select(AiMessage.message_id).where(
                    AiMessage.tenant_id == tenant.tenant_id,
                    AiMessage.conversation_id == action.conversation_id,
                    AiMessage.role == "assistant",
                    AiMessage.trace_id == action.trace_id,
                    AiMessage.is_active.is_(True),
                )
            )
            ok = status == PreparedActionStatus.SUCCEEDED
            lineage = result_projection_service.lineage_from_record(action)
            if ok:
                allowed = bool(
                    current_user is not None
                    and await result_projection_service.authorize_result_projection(
                        terminal_db,
                        current_user,
                        owner_user_id=action.user_id,
                        lineage=lineage,
                    )
                )
                if not allowed:
                    return [
                        AiErrorEvent(
                            error_code="AI_RESULT_PROJECTION_FORBIDDEN",
                            message="当前权限不允许读取该操作结果",
                        ),
                        DoneEvent(
                            trace_id=action.trace_id,
                            message_id=message_id,
                            persistence="committed",
                            projection="updated",
                        ),
                    ]
            result_data = action.result_data if ok else None
            result_ui = action.result_ui if ok else None
            if current_user is not None and lineage is not None and ok:
                export_ids: list[str] = []
                if action.execute_tool_name == "user.export" and isinstance(
                    result_data, dict
                ):
                    export_id = result_data.get("exportId") or result_data.get(
                        "export_id"
                    )
                    if export_id:
                        export_ids.append(str(export_id))
                result_data = await result_projection_service.refresh_download_urls(
                    terminal_db,
                    current_user,
                    tenant=tenant,
                    lineage=lineage,
                    value=result_data,
                    resource_ids=export_ids,
                )
                result_ui = await result_projection_service.refresh_download_urls(
                    terminal_db,
                    current_user,
                    tenant=tenant,
                    lineage=lineage,
                    value=result_ui,
                    resource_ids=export_ids,
                )
            fallback_error = {
                PreparedActionStatus.REJECTED: "USER_REJECTED",
                PreparedActionStatus.EXPIRED: "AI_HITL_EXPIRED",
                PreparedActionStatus.FAILED: "AI_INTERNAL_ERROR",
            }.get(status)
            result_event = ToolCallResultEvent(
                tool=action.execute_tool_name,
                tool_call_id=action.execute_tool_call_id,
                ok=ok,
                duration_ms=action.duration_ms or 0,
                result=result_data,
                error_code=None if ok else action.error_code or fallback_error,
                error_msg=(
                    None
                    if ok
                    else {
                        PreparedActionStatus.REJECTED: "用户已取消此操作",
                        PreparedActionStatus.EXPIRED: "确认已过期，请重新发起",
                        PreparedActionStatus.FAILED: "工具执行失败，请稍后重试",
                    }.get(status, "操作执行失败")
                ),
                ui=_ui_result_from_dict(result_ui) if ok else None,
            )
            done_event = DoneEvent(
                trace_id=action.trace_id,
                message_id=message_id,
                persistence="committed",
                projection="updated",
            )
            return [result_event, done_event]
    except Exception as exc:
        logger.exception(
            "resume durable terminal projection failed",
            extra={"confirmation_id": confirmation_id, "action_id": pending.action_id},
        )
        error_code = getattr(exc, "error_code", "AI_PREPARED_ACTION_TERMINAL_MISSING")
        return [
            AiErrorEvent(
                error_code=error_code,
                message="操作终态读取失败，请刷新会话确认结果",
            ),
            DoneEvent(
                trace_id=pending.trace_id,
                persistence="failed",
                projection="updated",
            ),
        ]


async def _cleanup_durable_resume(action, *, tenant: TenantContext) -> None:  # noqa: ANN001
    """Best-effort cleanup after PostgreSQL has become the terminal authority."""
    try:
        if not PreparedActionStatus(action.status).is_terminal:
            async with AsyncSessionLocal() as cleanup_db:
                latest = await prepared_action_service.get_by_confirmation_id(
                    cleanup_db, action.confirmation_id, tenant=tenant
                )
            if latest is None or not PreparedActionStatus(latest.status).is_terminal:
                return
            action = latest
    except Exception:
        logger.exception("resume durable cleanup terminal check failed")
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
            logger.exception("resume durable conversation guard cleanup failed")
    try:
        await hitl_manager.delete_pending(
            redis_client, action.confirmation_id, tenant=tenant
        )
    except Exception:
        logger.exception("resume durable pending cleanup failed")


async def _terminalize_durable_resume_failure(
    *,
    confirmation_id: str,
    pending: PendingPayload,
    user_id: int,
    tenant: TenantContext,
    error_code: str,
    error_msg: str,
) -> list[ToolCallResultEvent | AiErrorEvent | DoneEvent]:
    """Expire a still-pending durable action; never fall back to tool execution."""
    terminal_action = None
    try:
        async with AsyncSessionLocal() as terminal_db:
            async with terminal_db.begin():
                action = await prepared_action_service.get_by_confirmation_id(
                    terminal_db, confirmation_id, tenant=tenant
                )
                if not _durable_binding_valid(
                    action,
                    pending=pending,
                    user_id=user_id,
                    tenant=tenant,
                ):
                    raise BusinessRuleException(
                        "续传 action 绑定无效",
                        error_code="AI_PREPARED_ACTION_BINDING_INVALID",
                    )
                status = PreparedActionStatus(action.status)
                terminal_action = action
                if status == PreparedActionStatus.PENDING_CONFIRMATION:
                    transitioned = await prepared_action_service.transition_status(
                        terminal_db,
                        action_id=action.action_id,
                        expected_status=status,
                        expected_version=action.row_version,
                        target_status=PreparedActionStatus.EXPIRED,
                        error_code=error_code,
                        tenant=tenant,
                    )
                    if transitioned is not None:
                        terminal_action = transitioned
                        log = await operation_log_service.get_by_tool_call_id(
                            terminal_db,
                            transitioned.execute_tool_call_id,
                            user_id=user_id,
                            tenant=tenant,
                        )
                        if log is not None:
                            await operation_log_service.mark_expired_if_pending(
                                terminal_db, log.log_id, tenant=tenant
                            )
                        await chat_run_finalizer.finalize_prepared_action(
                            terminal_db,
                            action=transitioned,
                            ok=False,
                            error_code=error_code,
                            error_msg=error_msg,
                            tenant=tenant,
                        )
        if (
            terminal_action is not None
            and PreparedActionStatus(terminal_action.status).is_terminal
        ):
            await _cleanup_durable_resume(terminal_action, tenant=tenant)
        return await _load_durable_resume_terminal(
            confirmation_id=confirmation_id,
            pending=pending,
            user_id=user_id,
            tenant=tenant,
        )
    except Exception:
        logger.exception(
            "resume durable failure terminalization failed",
            extra={"confirmation_id": confirmation_id},
        )
        return [
            AiErrorEvent(
                error_code="AI_PREPARED_ACTION_TERMINAL_MISSING",
                message="操作状态暂不可用，请刷新会话确认结果",
            ),
            DoneEvent(
                trace_id=pending.trace_id,
                persistence="failed",
                projection="updated",
            ),
        ]


async def _finalize_resume_terminal(
    db: AsyncSession,
    *,
    confirmation_id: str,
    pending: PendingPayload,
    ok: bool,
    duration_ms: int = 0,
    result=None,  # noqa: ANN001
    error_code: str | None = None,
    error_msg: str | None = None,
    tenant: TenantContext,
) -> list[AiErrorEvent | DoneEvent]:
    """续传/离线 terminal projection 的 durability barrier。"""
    if pending.source_user_message_id is None:
        # 升级前 PendingPayload 无 durable source，不能猜测 parent；保留旧协议。
        try:
            await hitl_manager.delete_pending(
                redis_client, confirmation_id, tenant=tenant
            )
        except Exception:
            logger.exception("resume terminal legacy pending cleanup failed")
        return [DoneEvent()]
    try:
        message = await chat_run_finalizer.finalize_pending_turn(
            db,
            pending=pending,
            ok=ok,
            duration_ms=duration_ms,
            result=result,
            error_code=error_code,
            error_msg=error_msg,
            tenant=tenant,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception(
            "resume terminal finalization failed",
            extra={"trace_id": pending.trace_id, "tool_call_id": pending.tool_call_id},
        )
        return [
            AiErrorEvent(
                error_code="AI_MESSAGE_PERSIST_FAILED",
                message="工具终态持久化失败，请刷新会话确认状态",
            ),
            DoneEvent(
                trace_id=pending.trace_id,
                persistence="failed",
                projection="updated",
            ),
        ]
    if pending.guard_owner_token:
        try:
            await chat_run_guard.release(
                redis_client,
                conversation_id=pending.conversation_id,
                owner_token=pending.guard_owner_token,
                tenant=tenant,
            )
        except Exception:
            logger.exception("resume terminal conversation guard cleanup failed")
    try:
        await hitl_manager.delete_pending(redis_client, confirmation_id, tenant=tenant)
    except Exception:
        logger.exception("resume terminal pending cleanup failed")
    return [
        DoneEvent(
            trace_id=pending.trace_id,
            message_id=message.message_id if message else None,
            persistence="committed",
            projection="updated",
        )
    ]


async def _mark_legacy_resume_expired(
    db: AsyncSession,
    *,
    pending: PendingPayload,
    user_id: int,
    tenant: TenantContext,
) -> None:
    """Keep the legacy operation-log audit transition in the guarded stream."""
    try:
        log = await operation_log_service.get_by_tool_call_id(
            db,
            pending.tool_call_id,
            user_id=user_id,
            tenant=tenant,
        )
        if log is not None:
            await operation_log_service.mark_expired_if_pending(
                db, log.log_id, tenant=tenant
            )
    except Exception:
        logger.exception("resume: legacy mark_expired_if_pending failed")


@router.get("/resume", summary="SSE 流断流续传（HITL 期热接管）")
async def resume_chat(
    request: Request,
    confirmation_id_query: str | None = Query(
        default=None,
        alias="confirmation_id",
        description="调试后备（主推 Last-Event-ID 头）",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SSE 续传入口。

    读 confirmation_id 顺序：Last-Event-ID 头 > ?confirmation_id= query param
    """
    # 1. 功能开关校验（mode 不强校验：memory 模式单 worker 下 resume 同样可用，
    #    _hang_memory/_wake_memory 跨请求同进程工作；多 worker 部署需 redis_pubsub
    #    由部署配置保证，端点不重复强制校验。）
    if not settings.AI_SSE_RESUME_ENABLED:
        raise _set_exc_code(
            BusinessRuleException(
                "SSE 续传功能未启用", error_code="AI_RESUME_DISABLED"
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

    # 3. PostgreSQL 是 durable action/终态权威；Redis 仅承担活跃 handoff。
    tenant = get_bound_tenant_context(current_user)
    durable_action = await prepared_action_service.get_by_confirmation_id(
        db, confirmation_id, tenant=tenant
    )
    if durable_action is not None and (
        durable_action.user_id != current_user.user_id
        or durable_action.tenant_id != tenant.tenant_id
    ):
        raise AuthorizationException(error_code="AI_RESUME_FORBIDDEN")

    pending = await hitl_manager.get_pending(
        redis_client, confirmation_id, tenant=tenant
    )
    if pending is None and durable_action is None:
        raise NotFoundException("HITL confirmation", error_code="AI_RESUME_NOT_FOUND")
    if pending is not None and pending.user_id != current_user.user_id:
        logger.warning(
            "resume owner mismatch: confirmation_id=%s pending_user=%d current_user=%d",
            confirmation_id,
            pending.user_id,
            current_user.user_id,
        )
        raise AuthorizationException(error_code="AI_RESUME_FORBIDDEN")
    if pending is not None and pending.tenant_id != tenant.tenant_id:
        logger.warning(
            "resume tenant mismatch: confirmation_id=%s pending_tenant=%d current_tenant=%d",
            confirmation_id,
            pending.tenant_id,
            tenant.tenant_id,
        )
        # 与 owner mismatch 共用拒绝语义，避免泄露 pending 是否属于其它租户。
        raise AuthorizationException(error_code="AI_RESUME_FORBIDDEN")
    # PostgreSQL is the mode authority. During a rolling deployment an older
    # Redis payload may not contain action_id even though its durable Action has
    # already been committed; treating that payload as legacy would execute the
    # same tool a second time after POST /ai/confirm.
    if (
        durable_action is not None
        and pending is not None
        and not _durable_binding_valid(
            durable_action,
            pending=pending,
            user_id=current_user.user_id,
            tenant=tenant,
        )
    ):
        raise BusinessRuleException(
            "续传 action 绑定无效",
            error_code="AI_PREPARED_ACTION_BINDING_INVALID",
        )
    if durable_action is None and pending is not None and pending.action_id is not None:
        raise BusinessRuleException(
            "续传缺少已绑定的 durable action",
            error_code="AI_PREPARED_ACTION_BINDING_INVALID",
        )

    if not has_explicit_permission(current_user, AI_CHAT_USE_PERMISSION):
        return _minimal_resume_status(
            confirmation_id,
            durable_action=durable_action,
            pending=pending,
        )

    projection_allowed = False
    if durable_action is not None:
        projection_allowed = (
            await result_projection_service.authorize_result_projection(
                db,
                current_user,
                owner_user_id=durable_action.user_id,
                lineage=result_projection_service.lineage_from_record(durable_action),
            )
        )
    if not projection_allowed:
        return _minimal_resume_status(
            confirmation_id,
            durable_action=durable_action,
            pending=pending,
            error_code="AI_RESULT_PROJECTION_FORBIDDEN",
        )

    if (
        durable_action is not None
        and PreparedActionStatus(durable_action.status).is_terminal
    ):
        replay_pending = pending or _pending_from_durable_action(durable_action)
        resumed_event = _build_resumed_event(
            confirmation_id,
            replay_pending,
            durable_action=durable_action,
        )

        async def replay_terminal_stream():
            yield _format_sse_chunk(resumed_event)
            terminal_events = await _load_durable_resume_terminal(
                confirmation_id=confirmation_id,
                pending=replay_pending,
                user_id=current_user.user_id,
                tenant=tenant,
                current_user=current_user,
            )
            await _cleanup_durable_resume(durable_action, tenant=tenant)
            for terminal_event in terminal_events:
                yield _format_sse_chunk(terminal_event)

        return StreamingResponse(
            replay_terminal_stream(),
            media_type=SSE_CONTENT_TYPE,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if pending is None:
        # Durable 非终态但 hot handoff 已丢失，不能猜测执行；confirm 仍可按
        # PostgreSQL 权威收口，resume 维持稳定 not-found 拒绝面。
        raise NotFoundException("HITL confirmation", error_code="AI_RESUME_NOT_FOUND")

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

    ttl_sec = await hitl_manager.ttl(redis_client, confirmation_id, tenant=tenant)
    if ttl_sec < 60:
        raise _set_exc_code(
            BusinessRuleException(
                f"HITL 确认窗口剩余 {ttl_sec}s，已不足 60s",
                error_code="AI_RESUME_TTL_TOO_SHORT",
            ),
            422,
        )

    # 4. 获取 owner 锁，防止旧 worker 取消较慢时新 worker 重复执行。
    worker_token = secrets.token_urlsafe(16)
    lock_key = (
        f"{AI_HITL_OWNER_LOCK_PREFIX}:tenant:{tenant.tenant_id}:{confirmation_id}"
    )
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

    # 5. 构造 SSE 流（在 finally 释放锁）。Legacy-only DB/deps setup stays
    # inside the generator so every post-lock failure is covered by finally.
    resumed_event = _build_resumed_event(
        confirmation_id, pending, durable_action=durable_action
    )

    async def resume_stream():
        try:
            # 6.1 emit confirmation_resumed
            yield _format_sse_chunk(resumed_event)

            # 6.2 hang 等 wake
            try:
                action = await hitl_manager.hang(confirmation_id, tenant=tenant)
            except TimeoutError:
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code="AI_HITL_TIMEOUT",
                        message="HITL 确认超时（5min 无人确认），请重新发起",
                    )
                )
                if durable_action is not None:
                    terminal_events = await _terminalize_durable_resume_failure(
                        confirmation_id=confirmation_id,
                        pending=pending,
                        user_id=current_user.user_id,
                        tenant=tenant,
                        error_code="AI_HITL_TIMEOUT",
                        error_msg="HITL 确认超时（5min 无人确认）",
                    )
                else:
                    await _mark_legacy_resume_expired(
                        db,
                        pending=pending,
                        user_id=current_user.user_id,
                        tenant=tenant,
                    )
                    terminal_events = await _finalize_resume_terminal(
                        db,
                        confirmation_id=confirmation_id,
                        pending=pending,
                        ok=False,
                        error_code="AI_HITL_TIMEOUT",
                        error_msg="HITL 确认超时（5min 无人确认）",
                        tenant=tenant,
                    )
                for terminal_event in terminal_events:
                    yield _format_sse_chunk(terminal_event)
                return
            except Exception:
                # hang 抛非 Timeout 异常（如 Redis down）→ 先收口事实再 done
                logger.exception("resume: hang unexpected error")
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code="AI_INTERNAL_ERROR",
                        message="续传异常，请重新发起",
                    )
                )
                if durable_action is not None:
                    terminal_events = await _terminalize_durable_resume_failure(
                        confirmation_id=confirmation_id,
                        pending=pending,
                        user_id=current_user.user_id,
                        tenant=tenant,
                        error_code="AI_INTERNAL_ERROR",
                        error_msg="续传异常，请重新发起",
                    )
                else:
                    await _mark_legacy_resume_expired(
                        db,
                        pending=pending,
                        user_id=current_user.user_id,
                        tenant=tenant,
                    )
                    terminal_events = await _finalize_resume_terminal(
                        db,
                        confirmation_id=confirmation_id,
                        pending=pending,
                        ok=False,
                        error_code="AI_INTERNAL_ERROR",
                        error_msg="续传异常，请重新发起",
                        tenant=tenant,
                    )
                for terminal_event in terminal_events:
                    yield _format_sse_chunk(terminal_event)
                return

            # All new direct/prepared confirmations are executed exactly once by
            # POST /ai/confirm. A resumed waiter may only replay that committed
            # terminal projection; resume_tool_execution is legacy-only.
            if durable_action is not None:
                terminal_events = await _load_durable_resume_terminal(
                    confirmation_id=confirmation_id,
                    pending=pending,
                    user_id=current_user.user_id,
                    tenant=tenant,
                    current_user=current_user,
                )
                await _cleanup_durable_resume(durable_action, tenant=tenant)
                for terminal_event in terminal_events:
                    yield _format_sse_chunk(terminal_event)
                return

            try:
                log = await operation_log_service.get_by_tool_call_id(
                    db,
                    pending.tool_call_id,
                    user_id=current_user.user_id,
                    tenant=tenant,
                )
                log_id = log.log_id if log else None
            except Exception:
                logger.exception("resume: legacy operation log setup failed")
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code="AI_INTERNAL_ERROR",
                        message="续传初始化失败，请重新发起",
                    )
                )
                terminal_events = await _finalize_resume_terminal(
                    db,
                    confirmation_id=confirmation_id,
                    pending=pending,
                    ok=False,
                    error_code="AI_INTERNAL_ERROR",
                    error_msg="续传初始化失败，请重新发起",
                    tenant=tenant,
                )
                for terminal_event in terminal_events:
                    yield _format_sse_chunk(terminal_event)
                return

            # 6.3 REJECTED → mark_rejected + emit failure result
            if action == ConfirmAction.REJECTED:
                if log_id is not None:
                    try:
                        async with AsyncSessionLocal() as rej_db:
                            async with rej_db.begin():
                                await operation_log_service.mark_rejected(
                                    rej_db,
                                    log_id,
                                    approved_by=current_user.user_id,
                                    tenant=tenant,
                                )
                    except Exception:
                        logger.exception("resume: mark_rejected failed")
                yield _format_sse_chunk(
                    ToolCallResultEvent(
                        tool=pending.tool_name,
                        tool_call_id=pending.tool_call_id,
                        ok=False,
                        duration_ms=0,
                        error_code="USER_REJECTED",
                        error_msg="用户已取消此操作",
                    )
                )
                terminal_events = await _finalize_resume_terminal(
                    db,
                    confirmation_id=confirmation_id,
                    pending=pending,
                    ok=False,
                    error_code="USER_REJECTED",
                    error_msg="用户已取消此操作",
                    tenant=tenant,
                )
                for terminal_event in terminal_events:
                    yield _format_sse_chunk(terminal_event)
                return

            # 6.4 APPROVED → execute_tool
            if log_id is None:
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code="AI_INTERNAL_ERROR",
                        message="续传找不到原 log，请重新发起",
                    )
                )
                terminal_events = await _finalize_resume_terminal(
                    db,
                    confirmation_id=confirmation_id,
                    pending=pending,
                    ok=False,
                    error_code="AI_OPERATION_LOG_NOT_FOUND",
                    error_msg="续传找不到原 log，请重新发起",
                    tenant=tenant,
                )
                for terminal_event in terminal_events:
                    yield _format_sse_chunk(terminal_event)
                return

            try:
                deps = await chat_service.build_chat_deps(
                    db,
                    current_user,
                    agent_code=pending.agent_code,
                    trace_id=pending.trace_id,
                    conversation_id=pending.conversation_id,
                )
                deps.conversation_id = pending.conversation_id
                deps.source_user_message_id = pending.source_user_message_id
                deps.guard_owner_token = pending.guard_owner_token
                deps.command_action = pending.command_action
                deps.guard_handoff = True
            except BusinessException as exc:
                logger.info(
                    "resume: legacy authorization failed error_code=%s",
                    exc.error_code,
                )
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code=exc.error_code or "AI_AGENT_FORBIDDEN",
                        message=exc.message,
                    )
                )
                terminal_events = await _finalize_resume_terminal(
                    db,
                    confirmation_id=confirmation_id,
                    pending=pending,
                    ok=False,
                    error_code=exc.error_code or "AI_AGENT_FORBIDDEN",
                    error_msg=exc.message,
                    tenant=tenant,
                )
                for terminal_event in terminal_events:
                    yield _format_sse_chunk(terminal_event)
                return
            except Exception:
                logger.exception("resume: legacy ChatDeps setup failed")
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code="AI_INTERNAL_ERROR",
                        message="续传初始化失败，请重新发起",
                    )
                )
                terminal_events = await _finalize_resume_terminal(
                    db,
                    confirmation_id=confirmation_id,
                    pending=pending,
                    ok=False,
                    error_code="AI_INTERNAL_ERROR",
                    error_msg="续传初始化失败，请重新发起",
                    tenant=tenant,
                )
                for terminal_event in terminal_events:
                    yield _format_sse_chunk(terminal_event)
                return

            try:
                result, duration_ms = await resume_tool_execution(pending, deps, log_id)
            except Exception:
                logger.exception("resume: resume_tool_execution failed")
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code="AI_INTERNAL_ERROR",
                        message="续传 tool 执行失败，请重新发起",
                    )
                )
                terminal_events = await _finalize_resume_terminal(
                    db,
                    confirmation_id=confirmation_id,
                    pending=pending,
                    ok=False,
                    error_code="AI_INTERNAL_ERROR",
                    error_msg="续传 tool 执行失败，请重新发起",
                    tenant=tenant,
                )
                for terminal_event in terminal_events:
                    yield _format_sse_chunk(terminal_event)
                return

            # 6.5 emit tool_call_result + done
            result_event = ToolCallResultEvent(
                tool=pending.tool_name,
                tool_call_id=pending.tool_call_id,
                ok=result.ok,
                duration_ms=duration_ms,
                result=result.data if result.ok else None,
                error_code=result.error_code if not result.ok else None,
                error_msg=result.error_msg if not result.ok else None,
            )
            yield _format_sse_chunk(result_event)
            terminal_events = await _finalize_resume_terminal(
                db,
                confirmation_id=confirmation_id,
                pending=pending,
                ok=result.ok,
                duration_ms=duration_ms,
                result=result.data if result.ok else None,
                error_code=result.error_code if not result.ok else None,
                error_msg=result.error_msg if not result.ok else None,
                tenant=tenant,
            )
            for terminal_event in terminal_events:
                yield _format_sse_chunk(terminal_event)

        finally:
            # 释放 owner 锁（Lua 防误删）
            try:
                await redis_client.eval(_RELEASE_LOCK_LUA, 1, lock_key, worker_token)
            except Exception:
                logger.exception("resume: owner lock release failed")

    return StreamingResponse(
        resume_stream(),
        media_type=SSE_CONTENT_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
