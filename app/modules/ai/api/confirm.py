"""HITL 确认端点 — spec §8.3

POST /ai/confirm
  body: {confirmationId, action}
  → 200 {code: 200, msg: "success", data: {toolCallId, status: "queued"}}
  → 404 confirmation 不存在/已过期（CONFIRMATION_EXPIRED_OR_NOT_FOUND）
  → 403 非 owner（NOT_CONFIRMATION_OWNER）
  → 410 stream 已断（status="stream_gone"，前端进 SSE 断流轮询兜底）

工作流：
  1. 从 Redis 取 pending payload
  2. owner 校验（user_id 必须匹配）
  3. wake 进程内 asyncio.Event（如果 SSE 流还挂着）
  4. mark_approved / mark_rejected 写 ai_operation_log.approved_by（审计追责）
  5. 返回 tool_call_id，前端据此启动 30s 轮询（§9.3 GET /ai/operation-log）

注意：本端点只负责"唤醒"，真正执行在 Phase 3.2 的 Gateway Executor 里。
Phase 3.1 时 wake 会返回 False（没人 hang），前端进轮询兜底——但本端点仍写
approved_by，方便事后审计。
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import ResponseModel
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.redis import redis_client
from app.db.session import get_db
from app.modules.ai.agents.hitl.constants import ConfirmAction
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.schemas.confirm import ConfirmRequest, ConfirmResponse
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("", summary="HITL 工具调用确认")
async def confirm_tool(
    req: ConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ResponseModel[ConfirmResponse]:
    """用户在 HITL 抽屉点确认 / 取消

    spec §8.3：
      - confirmation_id 不可枚举（secrets.token_urlsafe(32)）
      - 5min TTL，过期自动 reject
      - 必须原会话所有者确认
    """
    # 1. 取 Redis pending
    pending = await hitl_manager.get_pending(redis_client, req.confirmation_id)
    if pending is None:
        # 不存在 / 已过期 / 服务重启清扫后
        raise NotFoundException(
            "HITL 确认", error_code="CONFIRMATION_EXPIRED_OR_NOT_FOUND"
        )

    # 2. owner 校验
    if pending.user_id != current_user.user_id:
        # 他人 token 尝试确认非自己会话的 HITL（spec §9.6 NOT_CONFIRMATION_OWNER）
        logger.warning(
            "HITL confirm owner mismatch: confirmation_id=%s pending_user=%d current_user=%d",
            req.confirmation_id,
            pending.user_id,
            current_user.user_id,
        )
        raise AuthorizationException(error_code="NOT_CONFIRMATION_OWNER")

    # 3. 写 ai_operation_log.approved_by（审计追责，无论 stream 是否还在）
    #    reject 也写 approved_by（§4.4 字段语义：按 confirm 的用户）
    log = await operation_log_service.get_by_tool_call_id(
        db, pending.tool_call_id, user_id=current_user.user_id
    )
    if log is not None:
        await operation_log_service.mark_approved(
            db, log.log_id, approved_by=current_user.user_id
        )
        await db.commit()

    # 4. 唤醒挂起的 SSE 流
    action = ConfirmAction(req.action)
    woken = await hitl_manager.wake(req.confirmation_id, action)

    if not woken:
        # stream 已断（SSE 流已结束 / 服务重启）
        # spec §8.5 MVP 简化：前端按 SSE 断流兜底处理
        # 这里仍返回 200 + status=stream_gone，前端进 30s 轮询兜底
        logger.info(
            "HITL confirm: stream already gone confirmation_id=%s tool_call_id=%s",
            req.confirmation_id,
            pending.tool_call_id,
        )
        return ResponseModel.success(
            data=ConfirmResponse(
                toolCallId=pending.tool_call_id,
                status="queued",  # 保持 status 不变（spec §8.3 契约）
            )
        )

    # 5. 唤醒成功，stream 会自己处理 approve/reject
    return ResponseModel.success(data=ConfirmResponse(toolCallId=pending.tool_call_id))


# 给 lint / IDE：避免未使用导入告警（BusinessRuleException 在 Phase 3.2 executor 接入时用）
_ = BusinessRuleException
