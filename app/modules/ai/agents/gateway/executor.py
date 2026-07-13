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

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationException, BusinessException
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
    decr_quota,
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

    spec §6.3 metric：每个 return 前用 _rec(status) 埋点
    ai_tool_calls_total{tool, status, risk, execution_mode}。
    """
    from app.modules.ai.metrics import record_tool_call  # noqa: PLC0415

    start = time.monotonic()
    # metric 用：闭包变量，每个 return 前更新；_rec 在 finally 风格不易做（多早期 return）
    metric_status = "failed"
    metric_risk = "unknown"
    metric_mode = "unknown"

    def _rec(status: str, *, risk: str | None = None, mode: str | None = None) -> None:
        record_tool_call(
            tool=name,
            status=status,
            risk=risk or metric_risk,
            execution_mode=mode or metric_mode,
            duration_sec=time.monotonic() - start,
        )

    registry = ToolRegistry.get()

    # 1. tool 存在性
    registered = registry.find(name)
    if registered is None:
        logger.warning(
            "tool not found",
            extra={"user_id": deps.user.user_id, "tool": name},
        )
        _rec("not_found")
        return ToolResult.failure(
            error_code="AI_TOOL_NOT_FOUND",
            error_msg=USER_FACING_MSG["AI_TOOL_NOT_FOUND"],
        )

    meta = registered.meta
    metric_risk = meta.risk
    user_id = deps.user.user_id

    # 2. 功能鉴权（spec §6.1）
    if not set(meta.required_perms) <= deps.perms:
        logger.warning(
            "perm denied via runtime check",
            extra={"user_id": user_id, "tool": name},
        )
        # §11.4 IP 拉黑计数（异步 fire-and-forget，不阻断主流程）
        await _record_perm_denied_for_ip(deps, name)
        _rec("perm_denied")
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
        _rec("super_admin_required")
        return ToolResult.failure(
            error_code="AI_SUPER_ADMIN_REQUIRED",
            error_msg="此操作仅超级管理员可执行，请联系超管或走传统界面",
        )

    # 3. 容量鉴权 L1/L2（仅写 tool，spec §6.4 + 修订 S-11）
    # check_l1_rate_limit 返回 (count, l1_member)，l1_member 用于业务函数内
    # 抛 AuthorizationException 时精确回滚（修订 S-11）
    l1_member: str | None = None
    if is_write_tool(meta):
        try:
            _, l1_member = await check_l1_rate_limit(redis_client, user_id)
            await check_l2_daily_quota(redis_client, user_id)
        except BusinessException as e:
            _rec("quota_rejected")
            return ToolResult.failure(error_code=e.error_code, error_msg=e.message)
        except RedisError:
            # spec §2.6: Redis 故障时写操作拒绝（保守降级，不静默放过）
            logger.exception(
                "Redis unavailable during quota check",
                extra={"user_id": user_id, "tool": name},
            )
            _rec("redis_down")
            return ToolResult.failure(
                error_code="AI_REDIS_DOWN",
                error_msg="AI 服务暂时不可用（容量校验失败），请稍后重试",
            )

    # 4. ai_operation_log 起始行（spec §6.5 修订 S-12：必须先写 log 再检查
    #    repeated_failure，否则 AI_REPEATED_FAILURE 路径漏审计行）
    # 修订 S-15：_start_log 失败 = 整 tool 调用失败（业务还没执行，不能漏审计行）
    args_hash = compute_args_hash(args)
    dry_run_count, dry_run_summary = await _run_dry_run(registered, args, deps)
    mode = classify_execution_mode(
        meta,
        dry_run_count=dry_run_count,
        injection_hit=deps.injection_hit,
    )
    metric_mode = mode.value
    tool_call_id = hitl_manager.generate_tool_call_id()
    summary = build_args_summary(
        meta.name,
        risk_level=meta.risk,
        execution_mode=mode.value,
        dry_run_count=dry_run_count,
    )
    try:
        log_id = await _start_log(
            deps, registered, tool_call_id, args_hash, summary, mode
        )
    except LogWriteError as e:
        # _start_log 重试 3 次仍失败 → 业务还没执行，必须终止避免漏审计行
        # 不抛给 LLM，转 ToolResult.failure
        logger.critical(
            "execute_tool aborted: _start_log failed, business NOT executed",
            extra={
                "user_id": user_id,
                "tool": name,
                "tool_call_id": tool_call_id,
                "error": str(e),
            },
        )
        _rec("internal_error")
        return ToolResult.failure(
            error_code="AI_INTERNAL_ERROR",
            error_msg="AI 服务暂时不可用（审计系统故障），请稍后重试",
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

    # 5. 连续失败兜底（spec §6.5）
    try:
        await check_repeated_failure(redis_client, user_id, name, args_hash)
    except BusinessException as e:
        # 修订 S-12：触发 AI_REPEATED_FAILURE 时 log 已在 step 4 写入，
        # 这里写终态 failed + AI_REPEATED_FAILURE（满足 spec §6.5
        # "连续失败兜底触发... 仍写一行 ai_operation_log"）
        await _finish_log_final(
            log_id,
            ToolResult.failure(
                error_code=e.error_code,
                error_msg=USER_FACING_MSG.get(e.error_code, e.message),
            ),
            started_at,
        )
        _rec("repeated_failure")
        return ToolResult.failure(
            error_code=e.error_code,
            error_msg=USER_FACING_MSG.get(e.error_code, e.message),
        )
    except RedisError:
        # spec §2.6: 连续失败检查也走 Redis，故障时保守降级（拒绝）
        logger.exception(
            "Redis unavailable during failure check",
            extra={"user_id": user_id, "tool": name},
        )
        await _finish_log_final(
            log_id,
            ToolResult.failure(
                error_code="AI_REDIS_DOWN",
                error_msg="AI 服务暂时不可用（安全检查失败），请稍后重试",
            ),
            started_at,
        )
        _rec("redis_down")
        return ToolResult.failure(
            error_code="AI_REDIS_DOWN",
            error_msg="AI 服务暂时不可用（安全检查失败），请稍后重试",
        )

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
            _rec("hitl_expired")
            return ToolResult.failure(
                error_code="AI_HITL_EXPIRED",
                error_msg=USER_FACING_MSG["AI_HITL_EXPIRED"],
            )
        if action == ConfirmAction.REJECTED:
            await _finish_log_rejected(log_id, user_id)
            _rec("user_rejected")
            return ToolResult.failure(
                error_code="USER_REJECTED",
                error_msg=USER_FACING_MSG["USER_REJECTED"],
            )
        # approved → mark_running 后继续执行
        await _finish_log_running(log_id)
        # 修订 S-3：HITL 等待结束，重置 started_at 为业务执行起点
        # duration_ms 只算业务耗时（不含 HITL 等待），hitl_wait_ms 在 mark_running 已写
        started_at = time.monotonic()

    # 8. 业务执行（修订 S-11：传 l1_member 进去，业务函数内抛
    #    AuthorizationException 时 decr_quota 精确回滚 L1 zset 成员）
    result = await _invoke_tool_fn(
        registered, args, deps, args_hash, l1_member=l1_member
    )

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

    metric_status = "success" if result.ok else "failed"
    _rec(metric_status)
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
    """写 ai_operation_log 行（spec §9.1，修订 S-15：失败必抛）

    initial status：autonomous → RUNNING；HITL → PENDING_CONFIRMATION

    修订 S-15：与 `_finish_log_final` 不同，`_start_log` 失败时业务**还没执行**，
    必须抛异常终止 execute_tool —— 否则业务执行了但无审计行，audit gap 比
    业务失败更严重。`_with_log_retry` 在 3 次重试后仍失败时抛
    `LogWriteError`，execute_tool 内部捕获并返回 ToolResult.failure。
    """
    initial_status = (
        AiOperationStatus.RUNNING
        if mode == AiExecutionMode.AUTONOMOUS
        else AiOperationStatus.PENDING_CONFIRMATION
    )

    async def _op(log_db: AsyncSession) -> int:
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

    return await _with_log_retry("start_operation", log_id=None, op=_op)


async def _finish_log_running(log_id: int) -> None:
    """HITL approved → status pending_confirmation → running（修订 S-15：3 次重试）"""

    async def _op(log_db: AsyncSession) -> None:
        await operation_log_service.mark_running(log_db, log_id)

    await _with_log_retry("mark_running", log_id=log_id, op=_op, raise_on_failure=False)


async def _finish_log_rejected(log_id: int, user_id: int) -> None:
    """HITL rejected → status pending_confirmation → rejected（修订 S-15：3 次重试）"""

    async def _op(log_db: AsyncSession) -> None:
        await operation_log_service.mark_rejected(log_db, log_id, approved_by=user_id)

    await _with_log_retry(
        "mark_rejected", log_id=log_id, op=_op, raise_on_failure=False
    )


async def _finish_log_final(log_id: int, result: ToolResult, started_at: float) -> None:
    """业务执行结束，写 log 终态（success / failed）（修订 S-15：3 次重试 + 不抛）

    修订 S-15 关键设计：
      - 业务事务（tool_db.begin()）已先于本函数返回时 commit/rollback，本函数
        写 log 失败**不**回滚业务（spec §6.3 "log 写入优先级低于业务"）
      - 3 次重试（0.5s / 1s / 1.5s backoff）后仍失败 → logger.critical
        （未来 Prometheus 接入时挂 `ai_log_write_failure_total{status}` counter）
      - 不抛异常：业务已成功 = 用户感知成功，审计 gap 走告警追查
    """
    duration_ms = int((time.monotonic() - started_at) * 1000)

    async def _op(log_db: AsyncSession) -> None:
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

    status_label = "success" if result.ok else "failed"
    await _with_log_retry(
        f"mark_{status_label}",
        log_id=log_id,
        op=_op,
        raise_on_failure=False,
    )


# ============ log 写入重试 helper（修订 S-15） ============


class LogWriteError(RuntimeError):
    """log 写入 3 次重试后仍失败。_start_log 路径必须抛此异常让 execute_tool 终止。"""


_LOG_RETRY_ATTEMPTS = 3
_LOG_RETRY_DELAYS_SEC = (0.5, 1.0, 1.5)  # 第 N 次失败后 sleep 的秒数


async def _with_log_retry(
    operation: str,
    *,
    log_id: int | None,
    op: Callable[[AsyncSession], Awaitable[Any]],
    raise_on_failure: bool = True,
) -> Any:
    """log 写入重试 helper（修订 S-15）

    Args:
        operation: 操作名（start_operation / mark_success / mark_failed / 等），
                   仅用于 log message
        log_id: 已知的 log_id（start_operation 路径还没 log_id 传 None）
        op: async 函数，接收 log_db session 执行业务
        raise_on_failure: True = 3 次失败后抛 LogWriteError（_start_log 用）；
                          False = 3 次失败后 logger.critical + 返回 None
                          （_finish_log_* 用，避免业务已成功后被审计拖垮）

    重试策略：
      - attempt 1: 立即执行
      - attempt 2: sleep 0.5s 后重试
      - attempt 3: sleep 1.0s 后重试
      - 仍失败: sleep 1.5s 不再重试，按 raise_on_failure 决定

    捕获的异常：DBAPIError / OperationalError / TimeoutError。其它异常（如
    ProgrammingError = SQL bug）直接抛，不浪费重试预算。
    """
    from sqlalchemy.exc import DBAPIError, OperationalError  # noqa: PLC0415

    last_exc: Exception | None = None
    for attempt in range(1, _LOG_RETRY_ATTEMPTS + 1):
        try:
            async with AsyncSessionLocal() as log_db:
                async with log_db.begin():
                    return await op(log_db)
        except (DBAPIError, OperationalError, TimeoutError) as e:
            last_exc = e
            if attempt < _LOG_RETRY_ATTEMPTS:
                delay = _LOG_RETRY_DELAYS_SEC[attempt - 1]
                logger.warning(
                    "ai_operation_log write failed, retrying",
                    extra={
                        "operation": operation,
                        "log_id": log_id,
                        "attempt": attempt,
                        "delay_sec": delay,
                        "error": str(e),
                    },
                )
                await asyncio.sleep(delay)

    # 3 次都失败
    logger.critical(
        "ai_operation_log 最终态写入失败 3 次（audit gap，业务结果以 tool 返回值为准）",
        extra={
            "operation": operation,
            "log_id": log_id,
            "error": str(last_exc),
            # Prometheus 接入后改为: counter ai_log_write_failure_total{operation}
        },
    )
    if raise_on_failure:
        raise LogWriteError(
            f"{operation} failed after {_LOG_RETRY_ATTEMPTS} attempts"
        ) from last_exc
    return None


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
    *,
    l1_member: str | None = None,
) -> ToolResult:
    """独立 session 内调用业务函数（spec §6.3）

    修订 S-11：业务函数内抛 AuthorizationException（典型场景：
    `ensure_targets_in_scope` 命中 data_scope 越界）时，必须 decr_quota
    回滚 L1/L2 计数器——否则用户被偷配额（spec §6.4 计数策略
    "data_scope 拒绝不计入"）。
    """
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
    except AuthorizationException as e:
        # 修订 S-11：data_scope 越界等授权失败，回滚 L1/L2 已写计数
        if is_write_tool(meta):
            try:
                await decr_quota(redis_client, user_id, l1_member=l1_member)
            except RedisError:
                # 回滚失败不阻断主流程（业务已失败，配额少 1 是次要问题）
                logger.exception(
                    "quota decr failed on AuthorizationException",
                    extra={"user_id": user_id, "tool": meta.name},
                )
        # §11.4 IP 拉黑计数（异步，不阻断主流程）
        await _record_perm_denied_for_ip(deps, meta.name)
        # 授权失败不计入失败计数（spec §6.4 计数策略）
        logger.info(
            "tool authorization denied (data_scope / etc)",
            extra={"user_id": user_id, "tool": meta.name, "error_code": e.error_code},
        )
        return ToolResult.failure(
            error_code=e.error_code or "AI_DATA_SCOPE_VIOLATION",
            error_msg=e.message if hasattr(e, "message") else str(e),
        )
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


# ============ §11.4 IP 拉黑计数（fire-and-forget） ============


async def _record_perm_denied_for_ip(deps: ChatDeps, tool_name: str) -> None:
    """鉴权拒绝时调 ip_blacklist.record_perm_denied（§11.4）

    fire-and-forget：开独立 session + 失败吞异常，绝不影响主流程。
    IP 来源：FastAPI request.client.host，由 chat.py 注入 deps（暂未注入则跳过）。
    """
    ip = getattr(deps, "client_ip", None)
    if not ip:
        return  # deps 未注入 client_ip（单元测试 / 旧路径），跳过
    try:
        from app.db.session import AsyncSessionLocal  # noqa: PLC0415
        from app.modules.ai.agents.safety.ip_blacklist import (  # noqa: PLC0415
            record_perm_denied,
        )

        async with AsyncSessionLocal() as db:
            await record_perm_denied(redis_client, db, ip)
    except Exception:
        logger.exception(
            "ip_blacklist record_perm_denied failed (ignored)",
            extra={"tool": tool_name, "ip": ip},
        )
