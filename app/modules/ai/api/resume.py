"""SSE 流断流续传端点 — spec §3 v1.5+

GET /ai/chat/resume
  - 读 Last-Event-ID 头作为 confirmation_id（SSE 协议标准）
  - 校验 owner / TTL
  - 抢 Redis owner 锁防双执行（spec §2.3）
  - emit confirmation_resumed → hang → execute_tool → emit tool_call_result + done
  - finally 释放 owner 锁（Lua 脚本防误删）

模式说明（不再硬校验）：
  - memory 模式：单 worker 本地开发可用。_hang_memory + _wake_memory 跨请求
    同进程工作，Event 在 confirm 端点被 set 后 hang 协程返回 action。
  - redis_pubsub 模式：多 worker 生产部署必需（spec §8.4）。SR-7 跨 worker wake
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
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.redis import redis_client
from app.db.session import AsyncSessionLocal, get_db
from app.modules.ai.agents.gateway.executor import resume_tool_execution
from app.modules.ai.agents.hitl.constants import (
    AI_HITL_OWNER_LOCK_PREFIX,
    AI_HITL_OWNER_LOCK_TTL_SEC,
    ConfirmAction,
)
from app.modules.ai.agents.hitl.events import (
    AiErrorEvent,
    ConfirmationResumedEvent,
    DoneEvent,
    DryRunSummary,
    ToolCallResultEvent,
)
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.api.chat import _format_sse_chunk
from app.modules.ai.service.chat_service import chat_service
from app.modules.ai.service.operation_log_service import operation_log_service
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
    """spec §9.6: BusinessRuleException 默认 code=400，手动改 code 返 409/410/422"""
    exc.code = code
    return exc


def _build_resumed_event(confirmation_id: str, pending) -> ConfirmationResumedEvent:  # noqa: ANN001
    """从 pending payload 构造 ConfirmationResumedEvent

    pending 是 PendingPayload dataclass。注意 confirmation_id 参数从 caller 传入
    （PendingPayload 不含 confirmation_id 字段，它是 Redis key 后缀）。
    """
    dry_run: DryRunSummary | None = None
    if pending.dry_run_result:
        dry_run = DryRunSummary(
            summary=pending.dry_run_result.get("summary", ""),
            affected_count=pending.dry_run_result.get("affected_count", 0),
        )
    return ConfirmationResumedEvent(
        confirmation_id=confirmation_id,
        tool=pending.tool_name,
        tool_call_id=pending.tool_call_id,
        summary=f"resume: tool={pending.tool_name}",
        args=pending.args,
        expires_at=pending.expires_at,
        resumed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        dry_run=dry_run,
    )


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
    """spec §3 v1.5+: SSE 续传入口

    读 confirmation_id 顺序：Last-Event-ID 头 > ?confirmation_id= query param
    """
    # 1. 功能开关校验（mode 不强校验：memory 模式单 worker 下 resume 同样可用，
    #    _hang_memory/_wake_memory 跨请求同进程工作；多 worker 部署需 redis_pubsub
    #    由部署文档 spec §8.4 约束，端点不强制）
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

    # 3. 取 Redis pending → 校验 owner + 已 wake + TTL
    pending = await hitl_manager.get_pending(redis_client, confirmation_id)
    if pending is None:
        raise NotFoundException("HITL confirmation", error_code="AI_RESUME_NOT_FOUND")
    if pending.user_id != current_user.user_id:
        logger.warning(
            "resume owner mismatch: confirmation_id=%s pending_user=%d current_user=%d",
            confirmation_id,
            pending.user_id,
            current_user.user_id,
        )
        raise AuthorizationException(error_code="AI_RESUME_FORBIDDEN")
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

    ttl_sec = await hitl_manager.ttl(redis_client, confirmation_id)
    if ttl_sec < 60:
        raise _set_exc_code(
            BusinessRuleException(
                f"HITL 确认窗口剩余 {ttl_sec}s，已不足 60s",
                error_code="AI_RESUME_TTL_TOO_SHORT",
            ),
            422,
        )

    # 4. 抢 owner 锁（防 worker A cancel 慢导致 worker B 双执行，spec §2.3）
    worker_token = secrets.token_urlsafe(16)
    lock_key = f"{AI_HITL_OWNER_LOCK_PREFIX}:{confirmation_id}"
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

    # 5. 查 log_id + 重建 ChatDeps（局部 import 防循环）
    log = await operation_log_service.get_by_tool_call_id(
        db, pending.tool_call_id, user_id=current_user.user_id
    )
    log_id = log.log_id if log else None

    deps = await chat_service.build_chat_deps(db, current_user, agent_code=None)
    deps.conversation_id = pending.conversation_id

    # 6. 构造 SSE 流（在 finally 释放锁）
    resumed_event = _build_resumed_event(confirmation_id, pending)

    async def resume_stream():
        try:
            # 6.1 emit confirmation_resumed
            yield _format_sse_chunk(resumed_event)

            # 6.2 hang 等 wake
            try:
                action = await hitl_manager.hang(confirmation_id)
            except TimeoutError:
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code="AI_HITL_TIMEOUT",
                        message="HITL 确认超时（5min 无人确认），请重新发起",
                    )
                )
                yield _format_sse_chunk(DoneEvent())
                if log_id is not None:
                    try:
                        async with AsyncSessionLocal() as cleanup_db:
                            async with cleanup_db.begin():
                                await operation_log_service.mark_expired_if_pending(
                                    cleanup_db, log_id
                                )
                    except Exception:
                        logger.exception("resume: mark_expired_if_pending failed")
                return
            except Exception:
                # hang 抛非 Timeout 异常（如 Redis down）→ emit error + done
                logger.exception("resume: hang unexpected error")
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code="AI_INTERNAL_ERROR",
                        message="续传异常，请重新发起",
                    )
                )
                yield _format_sse_chunk(DoneEvent())
                return

            # 6.3 REJECTED → mark_rejected + emit failure result
            if action == ConfirmAction.REJECTED:
                if log_id is not None:
                    try:
                        async with AsyncSessionLocal() as rej_db:
                            async with rej_db.begin():
                                await operation_log_service.mark_rejected(
                                    rej_db, log_id, approved_by=current_user.user_id
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
                yield _format_sse_chunk(DoneEvent())
                return

            # 6.4 APPROVED → execute_tool
            if log_id is None:
                yield _format_sse_chunk(
                    AiErrorEvent(
                        error_code="AI_INTERNAL_ERROR",
                        message="续传找不到原 log，请重新发起",
                    )
                )
                yield _format_sse_chunk(DoneEvent())
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
                yield _format_sse_chunk(DoneEvent())
                return

            # 6.5 emit tool_call_result + done
            yield _format_sse_chunk(
                ToolCallResultEvent(
                    tool=pending.tool_name,
                    tool_call_id=pending.tool_call_id,
                    ok=result.ok,
                    duration_ms=duration_ms,
                    result=result.data if result.ok else None,
                    error_code=result.error_code if not result.ok else None,
                    error_msg=result.error_msg if not result.ok else None,
                )
            )
            yield _format_sse_chunk(DoneEvent())

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
