"""Terminal SSE projection from facts verified by the fixed lifecycle owner."""

from sqlalchemy import select

from app.modules.ai.agents.gateway.result import ResultProjection, UIResult
from app.modules.ai.agents.hitl.constants import PreparedActionStatus
from app.modules.ai.agents.hitl.events import (
    AiErrorEvent,
    DoneEvent,
    ToolCallResultEvent,
)
from app.modules.ai.models.message import AiMessage
from app.modules.ai.service.result_projection_service import result_projection_service


def ui_result(value):
    if value is None:
        return None
    return UIResult(
        view_type=str(value.get("viewType") or value.get("view_type") or "plain_json"),
        view_data=value.get("viewData") or value.get("view_data") or {},
        audit=value.get("audit") or {},
        label_key=str(value.get("labelKey") or value.get("label_key") or ""),
        label_params=value.get("labelParams") or value.get("label_params") or {},
    )


async def project_resume_terminal(db, action, current_user, tenant):
    status = PreparedActionStatus(action.status)
    message_id = await db.scalar(
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
                db,
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
                    persistence="committed" if message_id else "not_applicable",
                    projection="updated" if message_id else "unchanged",
                ),
            ]
    result_data = action.result_data if ok else None
    result_ui = action.result_ui if ok else None
    if current_user is not None and lineage is not None and ok:
        export_ids: list[str] = []
        if action.execute_tool_name == "user.export" and isinstance(result_data, dict):
            export_id = result_data.get("exportId") or result_data.get("export_id")
            if export_id:
                export_ids.append(str(export_id))
        result_data = await result_projection_service.refresh_download_urls(
            db,
            current_user,
            tenant=tenant,
            lineage=lineage,
            value=result_data,
            resource_ids=export_ids,
        )
        result_ui = await result_projection_service.refresh_download_urls(
            db,
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
        ui=ui_result(result_ui) if ok else None,
        projection=ResultProjection(
            subject_refs=tuple(action.subject_refs or ()),
            scope_bound=action.data_scope_hash is not None,
        )
        if ok
        else None,
    )
    done_event = DoneEvent(
        trace_id=action.trace_id,
        message_id=message_id,
        persistence="committed" if message_id else "not_applicable",
        projection="updated" if message_id else "unchanged",
    )
    return [result_event, done_event]
