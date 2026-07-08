"""Gateway Executor — 统一 tool 执行入口（spec §3 / §6 / §8.2）

Phase 3.2 完整流程（HITL + 流式协议 + 审计）：

  1. tool 存在性 + 功能鉴权（perm）
  2. 容量鉴权 L1/L2（仅写 tool）+ 连续失败兜底（§6.4/§6.5）
  3. emit tool_call_started（spec §8.1）
  4. 写 ai_operation_log 行（initial status 由 mode 决定）
  5. risk classification（§5.3）+ dry_run 调用拿 count
  6. HITL 分支：
       a. create_pending + attach_confirmation + emit confirmation_required
       b. hang(confirmation_id) — 阻塞等 wake 或 5min TTL 超时
       c. wake 后：mark_running（approved）/ mark_rejected（rejected）/ mark_expired（timeout）
  7. 业务执行（独立 session + L3 超时 + serialize_for_llm 脱敏）
  8. emit tool_call_result + 写 log mark_success/failed

ChatDeps.signal_event 注入 SSE 事件回调，事件按 spec §8.1 流式协议 emit。
"""

import logging
import time
from typing import Any

from app.core.exceptions import BusinessException
from app.core.rbac import is_super_admin
from app.core.redis import redis_client
from app.db.session import AsyncSessionLocal
from app.modules.ai.agents.gateway.failures import (
    check_repeated_failure,
    clear_failures,
    compute_args_hash,
    record_failure,
)
from app.modules.ai.agents.gateway.quota import (
    check_l1_rate_limit,
    check_l2_daily_quota,
    is_write_tool,
    with_l3_timeout,
)
from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.agents.gateway.sensitive import serialize_for_llm
from app.modules.ai.agents.hitl.constants import (
    AiExecutionMode,
    AiOperationStatus,
    ConfirmAction,
    DryRunResult,
)
from app.modules.ai.agents.hitl.events import (
    AiStreamEvent,
    ConfirmationRequiredEvent,
    DryRunSummary,
    ToolCallResultEvent,
    ToolCallStartedEvent,
)
from app.modules.ai.agents.hitl.manager import hitl_manager
from app.modules.ai.agents.hitl.risk import classify_execution_mode
from app.modules.ai.agents.safety.auto_disable import record_injection
from app.modules.ai.agents.tools.registry import RegisteredTool, ToolRegistry
from app.modules.ai.core.context import ChatDeps, build_tool_context
from app.modules.ai.service.operation_log_service import operation_log_service

logger = logging.getLogger(__name__)

USER_FACING_MSG: dict[str, str] = {
    "AI_TOOL_NOT_FOUND": "该工具不在当前助手范围内，请换种方式问。",
    "AI_TOOL_PERM_DENIED": "你没有调用此工具的权限，请联系管理员。",
    "AI_DATA_SCOPE_VIOLATION": "目标不在你的可见范围内，请确认目标 ID 或联系管理员扩权。",
    "AI_RATE_LIMIT_USER_WRITE": "操作过于频繁，请稍后再试。",
    "AI_DAILY_QUOTA_EXHAUSTED": "今日配额已用尽，请明天再试。",
    "AI_TOOL_TIMEOUT": "操作超时，请稍后重试或拆分任务。",
    "AI_REPEATED_FAILURE": "相同操作已连续失败多次，建议换种方式或走传统界面。",
    "AI_INTERNAL_ERROR": "内部错误，请稍后重试。",
    "AI_HITL_EXPIRED": "操作超时未确认，请重新发起。",
    "USER_REJECTED": "用户已取消此操作。",
}


def build_args_summary(
    tool_name: str,
    *,
    risk_level: str,
    execution_mode: str,
    dry_run_count: int | None,
) -> str:
    """spec §9.2: 仅元信息，不含 args 字段值（白名单 = 泄漏面）"""
    parts = [f"tool={tool_name}", f"risk={risk_level}", f"mode={execution_mode}"]
    if dry_run_count is not None:
        parts.append(f"dry_run_count={dry_run_count}")
    return ", ".join(parts)


async def _emit(deps: ChatDeps, event: AiStreamEvent) -> None:
    """emit SSE 事件；deps.signal_event=None 时静默丢弃（单元测试场景）"""
    if deps.signal_event is not None:
        await deps.signal_event(event)


# 写 tool 的标准返回字段（result.data 是 dict 时按此优先级取影响行数）
_AFFECTED_ROW_KEYS: tuple[str, ...] = (
    "affected_count",
    "affected_rows",
    "count",
    "total",
    "groups_count",
)


def _infer_affected_rows(*, dry_run_count: int | None, result_data: Any) -> int | None:
    """从 dry_run_count 或 result.data 推断影响行数（spec §8.1 卡片状态文本用）

    优先级：
      1. dry_run_count（写 tool HITL 路径已精确算出，最权威）
      2. result_data 是 dict 且含 _AFFECTED_ROW_KEYS 任一字段 → 取整数值
      3. result_data 是 list → 长度
      4. 否则 None（前端隐藏「N 行」尾部）

    spec §5.5 readonly tool 直接返回 [{group, count}] 或 dict，这里 best-effort
    提取，tool fn 不强求字段命名约定。
    """
    if dry_run_count is not None:
        return dry_run_count
    if isinstance(result_data, dict):
        for key in _AFFECTED_ROW_KEYS:
            val = result_data.get(key)
            if isinstance(val, int) and not isinstance(val, bool):
                return val
    if isinstance(result_data, list):
        return len(result_data)
    return None


async def execute_tool(
    name: str,
    args: dict[str, Any],
    deps: ChatDeps,
) -> ToolResult:
    """统一 tool 执行入口（spec §3 / §6 / §8.2）

    Returns:
        ToolResult.success / ToolResult.failure
        不抛业务异常给上游 LLM，SSE 流不被中断
    """
    registry = ToolRegistry.get()

    # 1. tool 存在性
    registered = registry.find(name)
    if registered is None:
        logger.warning(
            "tool not found",
            extra={"user_id": deps.user.user_id, "tool": name},
        )
        return ToolResult.failure(
            error_code="AI_TOOL_NOT_FOUND",
            error_msg=USER_FACING_MSG["AI_TOOL_NOT_FOUND"],
        )

    meta = registered.meta
    user_id = deps.user.user_id

    # 2. 功能鉴权（spec §6.1）
    if not set(meta.required_perms) <= deps.perms:
        logger.warning(
            "perm denied via runtime check",
            extra={"user_id": user_id, "tool": name},
        )
        return ToolResult.failure(
            error_code="AI_TOOL_PERM_DENIED",
            error_msg=USER_FACING_MSG["AI_TOOL_PERM_DENIED"],
        )

    # 2b. super_admin gate（spec §11.2）：仅超管可调，短路在所有其他检查前
    if meta.super_admin_only and not is_super_admin(deps.user):
        logger.warning(
            "super_admin_only gate denied",
            extra={"user_id": user_id, "tool": name, "user_name": deps.user.user_name},
        )
        return ToolResult.failure(
            error_code="AI_SUPER_ADMIN_REQUIRED",
            error_msg="此操作仅超级管理员可执行，请联系超管或走传统界面",
        )

    # 3. 容量鉴权 L1/L2（仅写 tool，spec §6.4）
    if is_write_tool(meta):
        try:
            await check_l1_rate_limit(redis_client, user_id)
            await check_l2_daily_quota(redis_client, user_id)
        except BusinessException as e:
            return ToolResult.failure(error_code=e.error_code, error_msg=e.message)

    # 4. 连续失败兜底（spec §6.5）
    args_hash = compute_args_hash(args)
    try:
        await check_repeated_failure(redis_client, user_id, name, args_hash)
    except BusinessException as e:
        return ToolResult.failure(
            error_code=e.error_code,
            error_msg=USER_FACING_MSG.get(e.error_code, e.message),
        )

    # 5. dry_run + risk classification（spec §5.3 + §11.1 injection_hit）
    dry_run_count, dry_run_summary = await _run_dry_run(registered, args, deps)
    mode = classify_execution_mode(
        meta,
        dry_run_count=dry_run_count,
        injection_hit=deps.injection_hit,
    )

    # 6. tool_call_id + emit tool_call_started + 写 log（spec §8.1 / §9.1）
    tool_call_id = hitl_manager.generate_tool_call_id()
    summary = build_args_summary(
        meta.name,
        risk_level=meta.risk,
        execution_mode=mode.value,
        dry_run_count=dry_run_count,
    )
    started_at = time.monotonic()
    await _emit(
        deps,
        ToolCallStartedEvent(
            tool=meta.name,
            tool_call_id=tool_call_id,
            summary=summary,
            args=args,
            risk=meta.risk,
            trace_id=deps.trace_id,
        ),
    )
    log_id = await _start_log(deps, registered, tool_call_id, args_hash, summary, mode)

    # §11.4 用户级 injection 自动禁用（命中 ≥5/h 且非超管 → 禁用 24h）
    if deps.injection_hit:
        await record_injection(redis_client, deps.user)

    # 7. HITL 分支
    if mode == AiExecutionMode.HITL:
        action = await _hang_for_confirmation(
            deps, registered, log_id, tool_call_id, args, summary, dry_run_summary
        )
        if action is None:
            # 5min TTL 超时 → mark_expired（_hang_for_confirmation 内已迁移）
            return ToolResult.failure(
                error_code="AI_HITL_EXPIRED",
                error_msg=USER_FACING_MSG["AI_HITL_EXPIRED"],
            )
        if action == ConfirmAction.REJECTED:
            await _finish_log_rejected(log_id, user_id)
            return ToolResult.failure(
                error_code="USER_REJECTED",
                error_msg=USER_FACING_MSG["USER_REJECTED"],
            )
        # approved → mark_running 后继续执行
        await _finish_log_running(log_id)

    # 8. 业务执行
    result = await _invoke_tool_fn(registered, args, deps, args_hash)

    # 9. emit tool_call_result + 写 log 终态
    duration_ms = int((time.monotonic() - started_at) * 1000)
    await _emit(
        deps,
        ToolCallResultEvent(
            tool=meta.name,
            tool_call_id=tool_call_id,
            ok=result.ok,
            duration_ms=duration_ms,
            result=result.data if result.ok else None,
            affected_rows=_infer_affected_rows(
                dry_run_count=dry_run_count,
                result_data=result.data if result.ok else None,
            ),
            error_code=result.error_code if not result.ok else None,
            error_msg=result.error_msg if not result.ok else None,
        ),
    )
    await _finish_log_final(log_id, result, started_at)

    return result


# ============ dry_run ============


async def _run_dry_run(
    registered: RegisteredTool,
    args: dict[str, Any],
    deps: ChatDeps,
) -> tuple[int | None, DryRunSummary | None]:
    """调 dry_run_fn 拿 count（spec §5.3 风险分级用）

    Returns:
        (count, summary) — count None 表示未跑或失败；summary 含 reason 给 HITL 抽屉
    """
    if registered.dry_run_fn is None:
        return None, None

    try:
        async with AsyncSessionLocal() as dry_db:
            async with dry_db.begin():
                dry_ctx = build_tool_context(deps, dry_db, registered.meta)
                dr: DryRunResult = await with_l3_timeout(
                    registered.dry_run_fn(dry_ctx, **args)
                )
        return dr.count, DryRunSummary(
            summary=dr.reason or f"将影响 {dr.count} 行",
            affected_count=dr.count,
        )
    except BusinessException as e:
        logger.info(
            "dry_run business exception",
            extra={"tool": registered.meta.name, "error_code": e.error_code},
        )
        return None, DryRunSummary(summary=f"预估失败：{e.message}", affected_count=0)
    except Exception:
        logger.exception(
            "dry_run unexpected error", extra={"tool": registered.meta.name}
        )
        return None, DryRunSummary(summary="预估失败（内部错误）", affected_count=0)


# ============ ai_operation_log 写入 ============


async def _start_log(
    deps: ChatDeps,
    registered: RegisteredTool,
    tool_call_id: str,
    args_hash: str,
    args_summary: str,
    mode: AiExecutionMode,
) -> int:
    """写 ai_operation_log 行（spec §9.1）

    initial status：autonomous → RUNNING；HITL → PENDING_CONFIRMATION
    """
    initial_status = (
        AiOperationStatus.RUNNING
        if mode == AiExecutionMode.AUTONOMOUS
        else AiOperationStatus.PENDING_CONFIRMATION
    )

    async with AsyncSessionLocal() as log_db:
        async with log_db.begin():
            return await operation_log_service.start_operation(
                log_db,
                trace_id=deps.trace_id,
                conversation_id=deps.conversation_id or 0,
                user_id=deps.user.user_id,
                tool_name=registered.meta.name,
                tool_call_id=tool_call_id,
                args_hash=args_hash,
                args_summary=args_summary,
                risk_level=registered.meta.risk,
                execution_mode=mode.value,
                status=initial_status,
                is_security_event=deps.injection_hit,
                event_type="injection_pattern_matched" if deps.injection_hit else None,
            )


async def _finish_log_running(log_id: int) -> None:
    async with AsyncSessionLocal() as log_db:
        async with log_db.begin():
            await operation_log_service.mark_running(log_db, log_id)


async def _finish_log_rejected(log_id: int, user_id: int) -> None:
    async with AsyncSessionLocal() as log_db:
        async with log_db.begin():
            await operation_log_service.mark_rejected(
                log_db, log_id, approved_by=user_id
            )


async def _finish_log_final(log_id: int, result: ToolResult, started_at: float) -> None:
    """业务执行结束，写 log 终态（success / failed）"""
    duration_ms = int((time.monotonic() - started_at) * 1000)
    async with AsyncSessionLocal() as log_db:
        async with log_db.begin():
            if result.ok:
                summary = (
                    f"ok, keys={sorted(result.data.keys())}"
                    if isinstance(result.data, dict)
                    else "ok"
                )
                await operation_log_service.mark_success(
                    log_db, log_id, result_summary=summary, duration_ms=duration_ms
                )
            else:
                await operation_log_service.mark_failed(
                    log_db,
                    log_id,
                    error_code=result.error_code or "AI_INTERNAL_ERROR",
                    duration_ms=duration_ms,
                )


# ============ HITL 挂起 / 唤醒（spec §8.3） ============


async def _hang_for_confirmation(
    deps: ChatDeps,
    registered: RegisteredTool,
    log_id: int,
    tool_call_id: str,
    args: dict[str, Any],
    summary: str,
    dry_run_summary: DryRunSummary | None,
) -> ConfirmAction | None:
    """HITL 流：create_pending → emit confirmation_required → hang

    Returns:
        ConfirmAction.APPROVED / REJECTED — 用户确认结果
        None — 已 expired（5min TTL 超时，log 已迁移到 EXPIRED）
    """
    meta = registered.meta
    confirmation_id = hitl_manager.generate_confirmation_id()
    payload = await hitl_manager.create_pending(
        redis_client,
        confirmation_id=confirmation_id,
        user_id=deps.user.user_id,
        conversation_id=deps.conversation_id or 0,
        tool_call_id=tool_call_id,
        trace_id=deps.trace_id,
        tool_name=meta.name,
        args=args,
        dry_run_result=_summary_to_dict(dry_run_summary),
    )

    # 回填 confirmation_id 到 log 行（spec §4.4）
    async with AsyncSessionLocal() as log_db:
        async with log_db.begin():
            await operation_log_service.attach_confirmation(
                log_db, log_id, confirmation_id
            )

    # emit confirmation_required（spec §8.1）
    await _emit(
        deps,
        ConfirmationRequiredEvent(
            confirmation_id=confirmation_id,
            tool=meta.name,
            tool_call_id=tool_call_id,
            summary=summary,
            args=args,
            expires_at=payload.expires_at,
            dry_run=dry_run_summary,
        ),
    )

    # 阻塞等 wake（spec §8.3）
    try:
        action = await hitl_manager.hang(confirmation_id)
    except TimeoutError:
        # 5min TTL 超时 → mark_expired
        async with AsyncSessionLocal() as log_db:
            async with log_db.begin():
                await operation_log_service.mark_expired(log_db, log_id)
        await hitl_manager.delete_pending(redis_client, confirmation_id)
        return None

    # 用户确认后清 Redis pending
    await hitl_manager.delete_pending(redis_client, confirmation_id)
    return action


def _summary_to_dict(s: DryRunSummary | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {"summary": s.summary, "affected_count": s.affected_count}


# ============ 业务执行（独立 session + L3 超时 + 脱敏） ============


async def _invoke_tool_fn(
    registered: RegisteredTool,
    args: dict[str, Any],
    deps: ChatDeps,
    args_hash: str,
) -> ToolResult:
    """独立 session 内调用业务函数（spec §6.3）"""
    meta = registered.meta
    tool_fn = registered.fn
    user_id = deps.user.user_id

    try:
        async with AsyncSessionLocal() as tool_db:
            async with tool_db.begin():
                tool_ctx = build_tool_context(deps, tool_db, meta)
                # L3 单 tool 超时包装
                result = await with_l3_timeout(tool_fn(tool_ctx, **args))
                # spec §7.3: 返回值脱敏后再给 LLM
                safe_data = serialize_for_llm(meta.sensitive_output, result)
                # spec §6.5: 成功路径清零失败计数
                await clear_failures(redis_client, user_id, meta.name, args_hash)
                # spec §8.7: readonly tool 写 query_cache 给 chip 跳转用
                if meta.readonly and meta.query_cache_module and deps.trace_id:
                    _safe_write_query_cache(meta, args, deps, user_id)
                return ToolResult.success(data=safe_data)
    except BusinessException as e:
        await record_failure(redis_client, user_id, meta.name, args_hash)
        logger.info(
            "tool business exception",
            extra={"user_id": user_id, "tool": meta.name, "error_code": e.error_code},
        )
        return ToolResult.failure(
            error_code=e.error_code or "AI_INTERNAL_ERROR",
            error_msg=e.message if hasattr(e, "message") else str(e),
        )
    except Exception as e:
        await record_failure(redis_client, user_id, meta.name, args_hash)
        logger.exception(
            "tool unexpected error",
            extra={"user_id": user_id, "tool": meta.name},
        )
        return ToolResult.failure(
            error_code="AI_INTERNAL_ERROR",
            error_msg=f"{USER_FACING_MSG['AI_INTERNAL_ERROR']}（{type(e).__name__}）",
        )


def _safe_write_query_cache(
    meta: Any, args: dict[str, Any], deps: ChatDeps, user_id: int
) -> None:
    """写 ai:query_cache hash（spec §8.7），filters 按 allowed_filters 白名单过滤

    失败静默——query_cache 是辅助功能，不能让 chip 跳转失败影响主流程。
    异步调度避免阻塞 LLM 响应。
    """
    import asyncio  # noqa: PLC0415

    from app.modules.ai.agents.hitl.query_cache import (  # noqa: PLC0415
        set_query_cache,
    )

    raw_filters = args.get("filters") or {}
    if not isinstance(raw_filters, dict):
        raw_filters = {}
    safe_filters = (
        {k: v for k, v in raw_filters.items() if k in meta.allowed_filters}
        if meta.allowed_filters
        else {}
    )

    async def _write() -> None:
        try:
            await set_query_cache(
                redis_client,
                trace_id=deps.trace_id,
                tool_name=meta.name,
                module=meta.query_cache_module,
                filters=safe_filters,
                user_id=user_id,
            )
        except Exception:
            logger.exception(
                "query_cache write failed (ignored)",
                extra={"tool": meta.name, "trace_id": deps.trace_id},
            )

    # fire-and-forget：当前 event loop 中调度，不阻塞
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_write())
    except RuntimeError:
        # 无 event loop（单元测试同步调用场景），直接跳过
        pass
