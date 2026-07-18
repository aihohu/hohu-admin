"""跨会话 HITL 恢复列表端点 — spec §14 v1.5+

GET /ai/pending-confirmations
  用途：进入 /ai/chat 页面 + 30s 心跳拉当前 user 的待确认 HITL 操作
  权限：本人（current_user）— 不需要 ai:trace:view（这是 self-service 端点）
  数据源：DB 主查 user_id+status=pending_confirmation → Redis GET 校验每个
          confirmation_id 还活着（防 DB 脏数据，spec §14 SR-14）

响应字段过滤：args_summary 是 spec §7.2 "仅元信息"，不含 args 原值。
args 详情走 attemptResume → SSE confirmation_required 流获取。
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import ResponseModel
from app.core.redis import redis_client
from app.db.session import get_db
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.schemas.operation_log import PendingConfirmationOut
from app.modules.ai.service.operation_log_service import operation_log_service
from app.modules.auth.service import get_current_user
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()


def _parse_expires_at(iso_str: str) -> datetime:
    """Redis pending.expires_at 是 ISO 8601 UTC（'%Y-%m-%dT%H:%M:%SZ'）→ naive datetime

    与 DB TIMESTAMP WITHOUT TIME ZONE 一致（避免序列化时区偏差）。
    """
    return datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ")


@router.get("", summary="列出当前用户的待确认 HITL 操作（跨会话恢复用）")
async def list_pending_confirmations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ResponseModel[list[PendingConfirmationOut]]:
    """spec §14 跨会话恢复：DB 主查 + Redis 校验双源

    流程：
      1. DB 拿 user_id 的 status=pending_confirmation 行（按 queued_at 降序）
      2. 对每行 Redis GET pending payload——不存在说明 Redis 已 expire 但 DB
         状态未迁移（脏数据），跳过
      3. 返回存活的列表（前端 banner 展示）
    """
    rows = await operation_log_service.list_pending_by_user(db, current_user.user_id)
    if not rows:
        return ResponseModel.success(data=[])

    result: list[PendingConfirmationOut] = []
    for row, conversation_title in rows:
        if not row.confirmation_id:
            # 防御性：DB pending_confirmation 行理论上必有 confirmation_id
            # （start_operation 时 attach），但保险起见跳过
            continue
        pending = await hitl_manager.get_pending(redis_client, row.confirmation_id)
        if pending is None:
            # Redis 已 expire / 重启清扫，DB 脏数据 → 跳过
            continue
        if pending.wake_action is not None:
            # §14: 已被 confirm 过（approved/rejected）的 pending，即使 DB 状态
            # 因 worker 死亡未迁移（memory 模式 race），也不再展示给用户——避免
            # banner 永远卡死。Redis pending TTL 5min 后自然清理，DB 状态由
            # mark_expired_if_pending 兜底迁移（confirm 端点 wake=False 路径）。
            continue
        result.append(
            PendingConfirmationOut(
                confirmation_id=row.confirmation_id,
                tool_call_id=row.tool_call_id,
                tool_name=row.tool_name,
                conversation_id=row.conversation_id,
                conversation_title=conversation_title,
                trace_id=row.trace_id,
                args_summary=row.args_summary,
                risk_level=row.risk_level,
                queued_at=row.queued_at,
                expires_at=_parse_expires_at(pending.expires_at),
            )
        )
    return ResponseModel.success(data=result)
