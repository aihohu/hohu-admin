"""HITL 确认端点 — spec §8.3（含 2026-07-10 修订 S-13 / S-14）

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
  - 2026-07-10 S-13：HITL 期间用户被 §11.4 自动禁用后仍可 POST /ai/confirm
    执行破坏性操作 → 加 check_user_disabled 阻断
  - 2026-07-10 S-14：wake 失败时返回 200+queued 误导前端轮询 30s → 改返回
    status="stream_gone" + mark_expired_if_pending 兜底审计；wake 实现要求
    防双击 race（spec §8.3 修订 wake 契约 + manager.py wake pop entry）
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
from app.core.tenant import resolve_tenant_id
from app.db.session import AsyncSessionLocal, get_db
from app.modules.ai.agents.hitl.constants import ConfirmAction
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.agents.safety.auto_disable import check_user_disabled
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
      - 修订 S-13：必须查 check_user_disabled
      - 修订 S-14：wake 失败时返回 status="stream_gone"
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
    current_tenant_id = resolve_tenant_id(current_user)
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

    # 3. 修订 S-13：用户禁用检查
    # HITL 挂起期间用户可能被 §11.4 自动禁用（注入命中 5 次/h），禁用后用户仍
    # 持有 confirmation_id 可直接 POST /ai/confirm，必须阻断。
    if await check_user_disabled(redis_client, current_user.user_id):
        logger.warning(
            "HITL confirm blocked: user auto-disabled confirmation_id=%s user_id=%d",
            req.confirmation_id,
            current_user.user_id,
        )
        raise AuthorizationException(
            "AI 已被禁用，无法确认操作",
            error_code="AI_USER_DISABLED",
        )

    # 4. 写 ai_operation_log.approved_by（审计追责，无论 stream 是否还在）
    #    reject 也写 approved_by（§4.4 字段语义：按 confirm 的用户）
    log = await operation_log_service.get_by_tool_call_id(
        db, pending.tool_call_id, user_id=current_user.user_id
    )
    if log is not None:
        await operation_log_service.mark_approved(
            db, log.log_id, approved_by=current_user.user_id
        )
        await db.commit()

    # 5. 唤醒挂起的 SSE 流
    action = ConfirmAction(req.action)
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
        # 独立 session 标 expired（避免污染主请求 session）
        if log is not None:
            try:
                async with AsyncSessionLocal() as cleanup_db:
                    async with cleanup_db.begin():
                        await operation_log_service.mark_expired_if_pending(
                            cleanup_db, log.log_id
                        )
            except Exception:
                # mark_expired 失败不阻断主响应（审计 gap 走告警追查）
                logger.exception(
                    "mark_expired_if_pending failed confirmation_id=%s log_id=%s",
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


# 给 lint / IDE：避免未使用导入告警（BusinessRuleException 在 Phase 3.2 executor 接入时用）
_ = BusinessRuleException
