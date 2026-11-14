"""Compatibility operation DTO projection; planned callers supply locked facts."""

import logging

from app.core.auth import has_explicit_permission
from app.core.base_response import ResponseModel
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    NotFoundException,
)
from app.core.rbac import is_super_admin
from app.db.authorization_plan_session import _Journal
from app.modules.ai.constants import AI_CHAT_USE_PERMISSION
from app.modules.ai.schemas.operation_log import (
    OperationLogOut,
    OperationLogStatusOut,
)
from app.modules.ai.service.completion_read_state_service import (
    UNKNOWN_CODE,
    completion_read_state_service,
    effective_log_state,
)
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.ai.service.prepared_action_service import prepared_action_service
from app.modules.system.service.delivery_read_context import load_delivery_context

AI_TRACE_VIEW_PERM = "ai:trace:view"

logger = logging.getLogger(__name__)


async def project_operation(tool_call_id, db, current_user, tenant, *, states=None):
    # 直接用 service 查（不做 owner 校验，下面统一做）
    log = await operation_log_service.get_by_tool_call_id(
        db,
        tool_call_id,
        tenant=tenant,
    )
    if log is None:
        action = await prepared_action_service.get_by_execute_tool_call_id(
            db, tool_call_id, tenant=tenant
        )
        if action is None:
            raise NotFoundException(
                "AI 操作日志", error_code="AI_OPERATION_LOG_NOT_FOUND"
            )
        if not (
            action.user_id == current_user.user_id
            or is_super_admin(current_user)
            or has_explicit_permission(current_user, AI_TRACE_VIEW_PERM)
        ):
            raise AuthorizationException(error_code="AI_OPERATION_LOG_FORBIDDEN")
        # A missing required audit is uncertainty, never a reason to retry the
        # business action. Expose no PA result, owner marker, or synthetic log.
        return ResponseModel.success(
            data=OperationLogStatusOut(
                toolCallId=tool_call_id,
                status="running",
                errorCode=UNKNOWN_CODE,
                finishedAt=None,
            )
        )

    # 权限：本人 / 超管 / ai:trace:view
    is_owner = log.user_id == current_user.user_id
    is_super = is_super_admin(current_user)
    has_perm = has_explicit_permission(current_user, AI_TRACE_VIEW_PERM)
    is_auditor = is_super or has_perm
    if not (is_owner or is_auditor):
        logger.info(
            "AI operation log query denied",
        )
        raise AuthorizationException(error_code="AI_OPERATION_LOG_FORBIDDEN")

    if states is None:
        states = await completion_read_state_service.read(
            tenant=tenant, tool_call_ids=[log.tool_call_id]
        )
    effective = effective_log_state(log, states)
    journal = getattr(db, "_authorization_journal", None)
    if (
        isinstance(journal, _Journal)
        and journal.handle is not None
        and (
            is_auditor or has_explicit_permission(current_user, AI_CHAT_USE_PERMISSION)
        )
    ):
        try:
            context = await load_delivery_context(
                db,
                journal.handle,
                tenant=tenant,
                version=current_user.auth_version,
                action="delivery.trace-list"
                if is_auditor
                else "delivery.operation-read",
                agents=True,
            )
            decisions = context.decisions(
                context.record("ai.operation_log", str(log.log_id), context.scopes)
            )
            # Polling status is a fixed completion protocol. Optional audit
            # metadata is governed independently of the business result body.
            values = {"tool_call_id": log.tool_call_id, "status": effective.status}
            for name in (
                "tool_name",
                "started_at",
                "finished_at",
                "duration_ms",
                "error_code",
            ):
                if decisions[name].allowed:
                    values[name] = getattr(
                        log if name in {"tool_name", "started_at"} else effective, name
                    )
            return ResponseModel.success(data=OperationLogOut.model_validate(values))
        except BusinessException:
            pass  # Fixed owner/auditor status remains available after revocation.

    return ResponseModel.success(
        data=OperationLogStatusOut(
            toolCallId=log.tool_call_id,
            status=effective.status,
            errorCode=effective.error_code
            if effective.unknown
            or effective.error_code
            in {
                "USER_REJECTED",
                "AI_HITL_EXPIRED",
                "AI_PREPARED_ACTION_CANCELLED",
                "AI_PREVIEW_REJECTED",
                "AI_PREPARED_ACTION_SOURCE_STALE",
                "AI_PREPARED_ACTION_REVALIDATION_FAILED",
            }
            else None,
            finishedAt=None,
        )
    )
