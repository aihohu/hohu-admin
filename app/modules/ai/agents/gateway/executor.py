"""Gateway Executor — 统一工具执行入口。

完整流程（人工确认 + 流式协议 + 审计）：

  1. tool 存在性 + 功能鉴权（perm）
  2. 容量鉴权 L1/L2（仅写工具）+ 连续失败兜底
  3. emit tool_call_started
  4. 写 ai_operation_log 行（initial status 由 mode 决定）
  5. risk classification + dry_run 调用拿 count
  6. HITL 分支：
       a. create_pending + attach_confirmation + emit confirmation_required
       b. hang(confirmation_id) — 阻塞等 wake 或 5min TTL 超时
       c. wake 后：mark_running（approved）/ mark_rejected（rejected）/ mark_expired（timeout）
  7. 业务执行（独立 session + L3 超时 + serialize_for_llm 脱敏）
  8. emit tool_call_result + 写 log mark_success/failed

ChatDeps.signal_event 注入 SSE 事件回调。
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
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
    check_l1_global_rate_limit,
    check_l1_rate_limit,
    check_l2_agent_quota,
    check_l2_daily_quota,
    check_l4_conv_budget,
    decr_quota,
    is_write_tool,
    with_l3_timeout,
)
from app.modules.ai.agents.gateway.result import (
    PreparedActionProposal,
    ResultProjection,
    ToolResult,
    UIResult,
)
from app.modules.ai.agents.gateway.sensitive import serialize_for_llm
from app.modules.ai.agents.hitl.constants import (
    AiExecutionMode,
    AiOperationStatus,
    ConfirmAction,
    DryRunResult,
    PreparedActionStatus,
)
from app.modules.ai.agents.hitl.events import (
    AiStreamEvent,
    ConfirmationRequiredEvent,
    DryRunSummary,
    ToolCallResultEvent,
    ToolCallStartedEvent,
)
from app.modules.ai.agents.hitl.manager import PendingPayload, hitl_manager
from app.modules.ai.agents.hitl.risk import classify_execution_mode
from app.modules.ai.agents.safety.auto_disable import record_injection
from app.modules.ai.agents.tools.meta import AiToolMeta
from app.modules.ai.agents.tools.registry import RegisteredTool, ToolRegistry
from app.modules.ai.core.context import ChatDeps, build_tool_context
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.ai.service.operation_log_service import (
    build_target_summary,
    operation_log_service,
)
from app.modules.ai.service.prepared_action_service import prepared_action_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PreparedExecutionContext:
    proposal: PreparedActionProposal
    prepare_tool_call_id: str
    prepare_tool_name: str


@dataclass(frozen=True)
class _ConfirmationResolution:
    confirmation_id: str
    decision: ConfirmAction


@dataclass(frozen=True)
class _DryRunOutcome:
    count: int | None = None
    summary: DryRunSummary | None = None
    failure: ToolResult | None = None
    authorization_denied: bool = False


USER_FACING_MSG: dict[str, str] = {
    "AI_TOOL_NOT_FOUND": "该工具不在当前助手范围内，请换种方式问。",
    "AI_TOOL_AGENT_MISMATCH": "该工具不属于当前助手，请切换到对应助手后重试。",
    "AI_TOOL_PERM_DENIED": "你没有调用此工具的权限，请联系管理员。",
    "AI_DATA_SCOPE_VIOLATION": "目标不在你的可见范围内，请确认目标 ID 或联系管理员扩权。",
    "AI_RATE_LIMIT_USER_WRITE": "操作过于频繁，请稍后再试。",
    "AI_DAILY_QUOTA_EXHAUSTED": "今日配额已用尽，请明天再试。",
    "AI_TOOL_TIMEOUT": "操作超时，请稍后重试或拆分任务。",
    "AI_REPEATED_FAILURE": "相同操作已连续失败多次，建议换种方式或走传统界面。",
    "AI_INTERNAL_ERROR": "内部错误，请稍后重试。",
    "AI_HITL_EXPIRED": "操作超时未确认，请重新发起。",
    "USER_REJECTED": "用户已取消此操作。",
    "AI_TOOL_NOT_AVAILABLE_TO_MODEL": "该工具只能由系统在批准后调用。",
    "AI_PREPARED_ACTION_REQUIRED": "该操作必须由已批准的预览授权发起。",
    "AI_PREPARED_OUTCOME_REQUIRED": "请明确本次只预览，或在预览后请求确认执行。",
    "AI_PREPARED_ACTION_INVALID": "预览结果无法生成安全确认，请重新发起。",
}


def build_args_summary(
    tool_name: str,
    *,
    risk_level: str,
    execution_mode: str,
    dry_run_count: int | None,
    args: dict[str, Any] | None = None,
    summary_fields: tuple[str, ...] = (),
) -> str:
    """构造仅包含执行元信息和显式白名单字段的参数摘要。

    Args:
        args: 调用方传入的 args dict；None（默认）= 不提取字段（MVP 行为）
        summary_fields: 业务方在 AiToolMeta.args_summary_fields 声明的白名单字段名；
                       默认空 tuple = 不提取任何字段（MVP 行为，向后兼容）

    返回格式：`tool=X, risk=Y, mode=Z, dry_run_count=N[, field1=val1, field2=val2]`
    字段值用 repr() 包裹（区分 str / int / None），便于审计阅读。
    """
    parts = [f"tool={tool_name}", f"risk={risk_level}", f"mode={execution_mode}"]
    if dry_run_count is not None:
        parts.append(f"dry_run_count={dry_run_count}")
    # 只提取业务方显式声明的字段，避免敏感参数进入操作日志。
    # 默认空 tuple → 不追加（MVP 行为）
    if args is not None and summary_fields:
        for field_name in summary_fields:
            if field_name in args:
                parts.append(f"{field_name}={args[field_name]!r}")
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


def _infer_affected_rows(
    *,
    dry_run_count: int | None,
    result_data: Any,
    ui_audit: dict[str, Any] | None = None,
) -> int | None:
    """从 dry_run_count、ui.audit 或 result_data 推断影响行数。

    优先级：
      1. dry_run_count（HITL 路径精确算出）
      2. ui.audit（业务方显式声明，最权威运行时值）
      3. result_data dict 含 _AFFECTED_ROW_KEYS 任一字段
      4. result_data list → len
      5. None
    """
    if dry_run_count is not None:
        return dry_run_count
    if ui_audit:
        for key in _AFFECTED_ROW_KEYS:
            val = ui_audit.get(key)
            if isinstance(val, int) and not isinstance(val, bool):
                return val
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
    """Public model/runtime entry; prepared capabilities are not injectable."""
    return await _execute_tool(name, args, deps)


async def _execute_tool(
    name: str,
    args: dict[str, Any],
    deps: ChatDeps,
    *,
    _prepared_action_context: _PreparedExecutionContext | None = None,
) -> ToolResult:
    """统一工具执行入口。

    Returns:
        ToolResult.success / ToolResult.failure
        不抛业务异常给上游 LLM，SSE 流不被中断

    每个返回分支都通过 _rec(status) 记录指标。
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

    # Agent 与 Tool 归属必须精确一致；shared 不再充当跨 Agent 旁路。
    if (
        deps.agent is None
        or not getattr(deps.agent, "enabled", False)
        or getattr(deps.agent, "code", None) != meta.agent
    ):
        logger.warning(
            "tool agent mismatch",
            extra={
                "user_id": user_id,
                "tool": name,
                "tool_agent": meta.agent,
                "runtime_agent": getattr(deps.agent, "code", None),
            },
        )
        _rec("agent_mismatch", risk=meta.risk)
        return ToolResult.failure(
            error_code="AI_TOOL_AGENT_MISMATCH",
            error_msg=USER_FACING_MSG["AI_TOOL_AGENT_MISMATCH"],
        )

    # Gateway-only execute 不允许模型/普通调用方猜名称直调。prepared preview
    # 只能通过本函数的内部递归传入安全 confirmation presentation。
    prepared_source = registry.prepared_source_for(name)
    context_source = (
        registry.prepared_source_for(
            name,
            prepare_name=_prepared_action_context.prepare_tool_name,
        )
        if _prepared_action_context is not None
        else None
    )
    prepared_context_valid = (
        _prepared_action_context is not None
        and context_source is not None
        and bool(_prepared_action_context.prepare_tool_call_id)
    )
    if not meta.llm_visible and not prepared_context_valid:
        error_code = (
            "AI_PREPARED_ACTION_REQUIRED"
            if prepared_source is not None
            else "AI_TOOL_NOT_AVAILABLE_TO_MODEL"
        )
        _rec("prepared_action_required", risk=meta.risk)
        return ToolResult.failure(
            error_code=error_code,
            error_msg=USER_FACING_MSG[error_code],
        )

    requested_outcome: str | None = None
    business_args = dict(args)
    if meta.interaction_flow == "prepared":
        requested_outcome = business_args.pop("requested_outcome", None)
        if requested_outcome not in {"preview_only", "execute_if_approved"}:
            _rec("invalid_prepared_outcome", risk=meta.risk)
            return ToolResult.failure(
                error_code="AI_PREPARED_OUTCOME_REQUIRED",
                error_msg=USER_FACING_MSG["AI_PREPARED_OUTCOME_REQUIRED"],
            )

    # 普通/preview 事件可展示模型参数；prepared execute 事件只展示 proposal
    # 中的 presentation，真实 frozen args 仅保存在服务端 pending payload。
    public_event_args = (
        _prepared_action_context.proposal.presentation
        if _prepared_action_context is not None
        else args
    )

    # 2. 功能鉴权
    if not set(meta.required_perms) <= deps.perms:
        logger.warning(
            "perm denied via runtime check",
            extra={"user_id": user_id, "tool": name},
        )
        # 异步记录来源 IP 的鉴权拒绝，不阻断主流程。
        await _record_perm_denied_for_ip(deps, name)
        _rec("perm_denied")
        return ToolResult.failure(
            error_code="AI_TOOL_PERM_DENIED",
            error_msg=USER_FACING_MSG["AI_TOOL_PERM_DENIED"],
        )

    # 2b. super_admin gate：仅超管可调，并在其他检查前短路。
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

    # 3. 写工具的用户级、全局、Agent 级和会话级容量鉴权。
    # check_l1_rate_limit 返回 (count, l1_member)，l1_member 用于业务函数内
    # 抛 AuthorizationException 时按成员精确回滚。
    # Agent 配置专属额度时，在用户日额度之后叠加检查。
    # agent_code 从已通过精确归属校验的 deps.agent.code 取。
    # 部署方可叠加全局速率限制和会话日预算。
    l1_member: str | None = None
    l1_global_member: str | None = None
    l4_conv_key_for_rollback: str | None = None
    agent_code_for_rollback: str | None = None
    if is_write_tool(meta):
        try:
            _, l1_member = await check_l1_rate_limit(redis_client, user_id)
            # 仅在部署方配置全局每分钟限制时启用。
            global_result = await check_l1_global_rate_limit(redis_client)
            if global_result is not None:
                _, l1_global_member = global_result
            await check_l2_daily_quota(redis_client, user_id)
            # 仅在当前 Agent 配置专属日额度时启用。
            agent_quota_limit = (
                getattr(deps.agent, "daily_quota_per_user", None)
                if deps.agent is not None
                else None
            )
            if agent_quota_limit is not None:
                await check_l2_agent_quota(
                    redis_client,
                    user_id,
                    deps.agent.code,
                    limit=agent_quota_limit,
                )
                # 标记：AuthorizationException 时需要回滚 per-agent key
                agent_code_for_rollback = deps.agent.code
            # 仅在已有会话且部署方配置会话预算时启用。
            conv_id = deps.conversation_id or 0
            l4_result = await check_l4_conv_budget(redis_client, conv_id)
            if l4_result is not None:
                _, l4_conv_key_for_rollback = l4_result
        except BusinessException as e:
            _rec("quota_rejected")
            return ToolResult.failure(error_code=e.error_code, error_msg=e.message)
        except RedisError:
            # Redis 故障时拒绝写操作，避免绕过配额保护。
            logger.exception(
                "Redis unavailable during quota check",
                extra={"user_id": user_id, "tool": name},
            )
            _rec("redis_down")
            return ToolResult.failure(
                error_code="AI_REDIS_DOWN",
                error_msg="AI 服务暂时不可用（容量校验失败），请稍后重试",
            )

    # 4. 必须先写 ai_operation_log 起始行，再执行可能短路的安全检查，
    #    repeated_failure，否则 AI_REPEATED_FAILURE 路径漏审计行）
    # 起始日志失败时终止工具调用；业务尚未执行，不能留下审计缺口。
    args_hash = compute_args_hash(business_args)
    dry_run_outcome = await _run_dry_run(registered, business_args, deps)
    dry_run_count = dry_run_outcome.count
    dry_run_summary = dry_run_outcome.summary
    # 风险偏好来自当前 Agent 配置。
    agent_risk_appetite = (
        getattr(deps.agent, "risk_appetite", "balanced")
        if deps.agent is not None
        else "balanced"
    )
    mode = classify_execution_mode(
        meta,
        dry_run_count=dry_run_count,
        injection_hit=deps.injection_hit,
        risk_appetite=agent_risk_appetite,
    )
    metric_mode = mode.value
    tool_call_id = hitl_manager.generate_tool_call_id()
    summary = build_args_summary(
        meta.name,
        risk_level=meta.risk,
        execution_mode=mode.value,
        dry_run_count=dry_run_count,
        args=business_args,
        summary_fields=meta.args_summary_fields,
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
            args=public_event_args,
            risk=meta.risk,
            trace_id=deps.trace_id,
            chip_target=meta.chip_target,
        ),
    )

    if dry_run_outcome.failure is not None:
        failure = dry_run_outcome.failure
        if dry_run_outcome.authorization_denied:
            if is_write_tool(meta):
                try:
                    await decr_quota(
                        redis_client,
                        user_id,
                        agent_code=agent_code_for_rollback,
                        l1_member=l1_member,
                        l1_global_member=l1_global_member,
                        l4_conv_key=l4_conv_key_for_rollback,
                    )
                except RedisError:
                    logger.exception(
                        "quota rollback failed after dry-run authorization denial",
                        extra={"user_id": user_id, "tool": meta.name},
                    )
            await _record_perm_denied_for_ip(deps, meta.name)
        else:
            await record_failure(redis_client, user_id, meta.name, args_hash)
        await _finish_log_final(log_id, failure, started_at)
        duration_ms = int((time.monotonic() - started_at) * 1000)
        await _emit(
            deps,
            ToolCallResultEvent(
                tool=meta.name,
                tool_call_id=tool_call_id,
                ok=False,
                duration_ms=duration_ms,
                error_code=failure.error_code,
                error_msg=failure.error_msg,
            ),
        )
        _rec("failed")
        return failure

    # 5. 相同参数连续失败时短路，避免重复消耗资源。
    try:
        await check_repeated_failure(redis_client, user_id, name, args_hash)
    except BusinessException as e:
        # 连续失败短路前已经写入起始日志，因此必须补齐终态。
        # 写入终态失败，确保审计日志与用户看到的短路结果一致。
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
        # Redis 故障时保守拒绝写操作。
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

    # 非超管用户一小时内多次命中注入检测后自动禁用 24 小时。
    if deps.injection_hit:
        await record_injection(redis_client, deps.user)

    # 7. HITL 分支
    if mode == AiExecutionMode.HITL:
        resolution = await _hang_for_confirmation(
            deps,
            registered,
            log_id,
            tool_call_id,
            business_args,
            summary,
            dry_run_summary,
            prepared_action_context=_prepared_action_context,
            agent_code_for_rollback=agent_code_for_rollback,
            l1_member=l1_member,
            l1_global_member=l1_global_member,
            l4_conv_key_for_rollback=l4_conv_key_for_rollback,
        )
        if resolution is None:
            # 5min TTL 超时 → mark_expired（_hang_for_confirmation 内已迁移）
            _rec("hitl_expired")
            return ToolResult.failure(
                error_code="AI_HITL_EXPIRED",
                error_msg=USER_FACING_MSG["AI_HITL_EXPIRED"],
            )
        action_result, action_duration = await _load_prepared_terminal_result(
            resolution.confirmation_id
        )
        await _emit(
            deps,
            ToolCallResultEvent(
                tool=meta.name,
                tool_call_id=tool_call_id,
                ok=action_result.ok,
                duration_ms=action_duration,
                result=action_result.data if action_result.ok else None,
                ui=action_result.ui if action_result.ok else None,
                projection=action_result.projection,
                error_code=action_result.error_code if not action_result.ok else None,
                error_msg=action_result.error_msg if not action_result.ok else None,
            ),
        )
        _rec("success" if action_result.ok else "failed")
        return action_result

    # 8. 业务执行；授权失败时按已写入的成员精确回滚各层配额。
    result = await _invoke_tool_fn(
        registered,
        business_args,
        deps,
        args_hash,
        agent_code_for_rollback=agent_code_for_rollback,
        l1_member=l1_member,
        l1_global_member=l1_global_member,
        l4_conv_key_for_rollback=l4_conv_key_for_rollback,
    )

    # 9. emit tool_call_result + 写 log 终态
    if result.ok and meta.interaction_flow == "prepared":
        if not isinstance(result.prepared_action, PreparedActionProposal):
            result = ToolResult.failure(
                error_code="AI_PREPARED_ACTION_INVALID",
                error_msg=USER_FACING_MSG["AI_PREPARED_ACTION_INVALID"],
            )
        elif requested_outcome == "preview_only":
            # A preview-only response is terminal. Consume the
            # internal proposal before returning so no caller can promote an
            # old preview into an executable action.
            result.prepared_action = None

    duration_ms = int((time.monotonic() - started_at) * 1000)
    # 只读工具不展示 affected_rows（user.count 返回
    # {"count": 42} 会被推断成 affected_rows=42，误导成「42 行受影响」）.
    # readonly = 查询类，无受影响行概念；强制 None 让前端隐藏「N 行」后缀.
    inferred_affected_rows = (
        _infer_affected_rows(
            dry_run_count=dry_run_count,
            result_data=result.data if result.ok else None,
            ui_audit=result.ui.audit if result.ok and result.ui else None,
        )
        if not meta.readonly
        else None
    )
    await _emit(
        deps,
        ToolCallResultEvent(
            tool=meta.name,
            tool_call_id=tool_call_id,
            ok=result.ok,
            duration_ms=duration_ms,
            result=result.data if result.ok else None,
            ui=result.ui if result.ok else None,
            projection=result.projection,
            affected_rows=inferred_affected_rows,
            error_code=result.error_code if not result.ok else None,
            error_msg=result.error_msg if not result.ok else None,
        ),
    )
    await _finish_log_final(log_id, result, started_at)

    metric_status = "success" if result.ok else "failed"
    _rec(metric_status)

    # 预览成功后由 Gateway 自动进入与执行工具绑定的人工确认；
    # wrapper 仍只代表模型的一次 preview tool call。proposal 不序列化给模型。
    if (
        result.ok
        and meta.interaction_flow == "prepared"
        and requested_outcome == "execute_if_approved"
    ):
        proposal = result.prepared_action
        if not isinstance(proposal, PreparedActionProposal):
            return ToolResult.failure(
                error_code="AI_PREPARED_ACTION_INVALID",
                error_msg=USER_FACING_MSG["AI_PREPARED_ACTION_INVALID"],
            )
        execute_name = meta.prepared_execute_tool
        if not execute_name:
            return ToolResult.failure(
                error_code="AI_PREPARED_ACTION_INVALID",
                error_msg=USER_FACING_MSG["AI_PREPARED_ACTION_INVALID"],
            )
        return await _execute_tool(
            execute_name,
            proposal.frozen_args,
            deps,
            _prepared_action_context=_PreparedExecutionContext(
                proposal=proposal,
                prepare_tool_call_id=tool_call_id,
                prepare_tool_name=meta.name,
            ),
        )
    return result


def validate_prepared_execution(
    action: AiPreparedAction,
    deps: ChatDeps,
) -> RegisteredTool:
    """Revalidate the frozen action against current Gateway policy."""
    registry = ToolRegistry.get()
    registered = registry.find(action.execute_tool_name)
    if action.interaction_flow == "prepared":
        source = registry.prepared_source_for(action.execute_tool_name)
        if registered is None or source is None or registered.meta.llm_visible:
            raise AuthorizationException(
                "Prepared execute capability is no longer available",
                error_code="AI_PREPARED_ACTION_CAPABILITY_UNAVAILABLE",
            )
    elif action.interaction_flow == "direct":
        if (
            registered is None
            or registered.meta.interaction_flow != "direct"
            or not registered.meta.llm_visible
        ):
            raise AuthorizationException(
                "Direct execute capability is no longer available",
                error_code="AI_PREPARED_ACTION_CAPABILITY_UNAVAILABLE",
            )
    else:
        raise AuthorizationException(
            "Action interaction flow is invalid",
            error_code="AI_PREPARED_ACTION_BINDING_INVALID",
        )
    if compute_args_hash(action.frozen_args) != action.args_hash:
        raise AuthorizationException(
            "Prepared action arguments failed integrity validation",
            error_code="AI_PREPARED_ACTION_BINDING_INVALID",
        )
    if (
        deps.agent is None
        or not deps.agent.enabled
        or deps.agent.code != action.agent_code
    ):
        raise AuthorizationException(
            "Prepared action agent is no longer enabled",
            error_code="AI_PREPARED_ACTION_AGENT_UNAVAILABLE",
        )
    if registered.meta.agent != action.agent_code:
        raise AuthorizationException(
            "Prepared action tool binding changed",
            error_code="AI_PREPARED_ACTION_BINDING_INVALID",
        )
    if not set(registered.meta.required_perms) <= deps.perms:
        raise AuthorizationException(
            "Current permissions no longer allow this operation",
            error_code="AI_TOOL_PERM_DENIED",
        )
    if registered.meta.super_admin_only and not is_super_admin(deps.user):
        raise AuthorizationException(
            "This operation requires a super administrator",
            error_code="AI_SUPER_ADMIN_REQUIRED",
        )
    return registered


async def execute_approved_prepared_action(
    action: AiPreparedAction,
    deps: ChatDeps,
) -> ToolResult:
    """Execute any CAS-claimed durable action without a second log/HITL cycle."""
    registered = validate_prepared_execution(action, deps)
    business_snapshot = (
        action.snapshot.get("business")
        if isinstance(action.snapshot, dict)
        and isinstance(action.snapshot.get("business"), dict)
        else None
    )
    return await _invoke_tool_fn(
        registered,
        dict(action.frozen_args),
        deps,
        action.args_hash,
        approved_business_snapshot=business_snapshot,
    )


async def _get_prepared_action_by_confirmation(
    confirmation_id: str,
) -> AiPreparedAction | None:
    async with AsyncSessionLocal() as db:
        return await prepared_action_service.get_by_confirmation_id(db, confirmation_id)


def _ui_from_dict(value: dict[str, Any] | None) -> UIResult | None:
    if not value:
        return None
    return UIResult(
        view_type=str(value.get("viewType") or value.get("view_type") or "plain_json"),
        view_data=dict(value.get("viewData") or value.get("view_data") or {}),
        audit=dict(value.get("audit") or {}),
        label_key=str(value.get("labelKey") or value.get("label_key") or ""),
        label_params=dict(value.get("labelParams") or value.get("label_params") or {}),
    )


async def _load_prepared_terminal_result(
    confirmation_id: str,
) -> tuple[ToolResult, int]:
    action = await _get_prepared_action_by_confirmation(confirmation_id)
    if action is None:
        return (
            ToolResult.failure(
                "AI_PREPARED_ACTION_NOT_FOUND",
                "Prepared action no longer exists",
            ),
            0,
        )
    status = PreparedActionStatus(action.status)
    if status == PreparedActionStatus.SUCCEEDED:
        return (
            ToolResult.success(
                action.result_data,
                ui=_ui_from_dict(action.result_ui),
                projection=ResultProjection(
                    subject_refs=tuple(action.subject_refs or ()),
                    scope_bound=action.data_scope_hash is not None,
                ),
            ),
            action.duration_ms or 0,
        )
    code = action.error_code or (
        "USER_REJECTED"
        if status == PreparedActionStatus.REJECTED
        else "AI_HITL_EXPIRED"
        if status == PreparedActionStatus.EXPIRED
        else "AI_PREPARED_ACTION_EXECUTION_INTERRUPTED"
    )
    return (
        ToolResult.failure(
            code, USER_FACING_MSG.get(code, "Operation did not complete")
        ),
        action.duration_ms or 0,
    )


# ============ dry_run ============


async def _run_dry_run(
    registered: RegisteredTool,
    args: dict[str, Any],
    deps: ChatDeps,
) -> _DryRunOutcome:
    """调用 dry_run_fn 获取风险判断和确认展示所需摘要。

    Returns:
        A typed outcome containing either the estimate or a terminal failure.
    """
    if not registered.meta.dry_run_supported or registered.dry_run_fn is None:
        return _DryRunOutcome()

    try:
        async with AsyncSessionLocal() as dry_db:
            async with dry_db.begin():
                dry_ctx = build_tool_context(deps, dry_db, registered.meta)
                dr: DryRunResult = await with_l3_timeout(
                    registered.dry_run_fn(dry_ctx, **args)
                )
        return _DryRunOutcome(
            count=dr.count,
            summary=DryRunSummary(
                summary=dr.reason or f"将影响 {dr.count} 行",
                affected_count=dr.count,
                summary_key=dr.summary_key,
                summary_params=dr.summary_params,
                affected_examples=dr.examples,
                confirmation_fields=dr.confirmation_fields,
                execution_args=dr.execution_args,
                business_snapshot=dr.business_snapshot,
            ),
        )
    except BusinessException as e:
        logger.info(
            "dry_run business exception",
            extra={"tool": registered.meta.name, "error_code": e.error_code},
        )
        return _DryRunOutcome(
            failure=ToolResult.failure(
                error_code=e.error_code or "AI_INTERNAL_ERROR",
                error_msg=e.message,
            ),
            authorization_denied=isinstance(e, AuthorizationException),
        )
    except Exception:
        logger.exception(
            "dry_run unexpected error", extra={"tool": registered.meta.name}
        )
        return _DryRunOutcome(
            failure=ToolResult.failure(
                error_code="AI_INTERNAL_ERROR",
                error_msg=USER_FACING_MSG["AI_INTERNAL_ERROR"],
            )
        )


# ============ ai_operation_log 写入 ============


async def _start_log(
    deps: ChatDeps,
    registered: RegisteredTool,
    tool_call_id: str,
    args_hash: str,
    args_summary: str,
    mode: AiExecutionMode,
) -> int:
    """写入 ai_operation_log 起始行；失败必须向上传播。

    initial status：autonomous → RUNNING；HITL → PENDING_CONFIRMATION

    与 `_finish_log_final` 不同，`_start_log` 失败时业务**还没执行**，
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
            tenant_id=deps.tenant_id,
            source_user_message_id=deps.source_user_message_id,
            readonly_snapshot=registered.meta.readonly,
            agent_code=deps.agent.code if deps.agent else registered.meta.agent,
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
    """HITL approved → status pending_confirmation → running，失败最多重试三次。"""

    async def _op(log_db: AsyncSession) -> None:
        await operation_log_service.mark_running(log_db, log_id)

    await _with_log_retry("mark_running", log_id=log_id, op=_op, raise_on_failure=False)


async def _finish_log_rejected(log_id: int, user_id: int) -> None:
    """HITL rejected → status pending_confirmation → rejected，失败最多重试三次。"""

    async def _op(log_db: AsyncSession) -> None:
        await operation_log_service.mark_rejected(log_db, log_id, approved_by=user_id)

    await _with_log_retry(
        "mark_rejected", log_id=log_id, op=_op, raise_on_failure=False
    )


async def _finish_log_final(log_id: int, result: ToolResult, started_at: float) -> None:
    """业务执行结束后写入日志终态，失败最多重试三次且不向业务传播。

    业务事务已先于本函数提交或回滚，因此终态日志失败不能反向回滚业务；
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
                log_db,
                log_id,
                result_summary=summary,
                duration_ms=duration_ms,
                target_summary=build_target_summary(
                    result.projection.subject_refs if result.projection else None
                ),
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


# ============ 日志写入重试 ============


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
    """带退避的日志写入重试。

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


# ============ 人工确认挂起 / 唤醒 ============


async def _rollback_failed_confirmation_setup(
    deps: ChatDeps,
    registered: RegisteredTool,
    confirmation_id: str,
    log_id: int,
    *,
    agent_code_for_rollback: str | None,
    l1_member: str | None,
    l1_global_member: str | None,
    l4_conv_key_for_rollback: str | None,
) -> None:
    """Best-effort rollback for a notification published before DB persistence."""
    try:
        await hitl_manager.delete_pending(redis_client, confirmation_id)
    except RedisError:
        logger.exception(
            "failed to delete pending after durable action setup failure",
            extra={"confirmation_id": confirmation_id},
        )

    if deps.guard_owner_token and deps.conversation_id:
        from app.modules.ai.service.chat_run_service import (  # noqa: PLC0415
            chat_run_guard,
        )

        try:
            await chat_run_guard.release(
                redis_client,
                conversation_id=deps.conversation_id,
                owner_token=deps.guard_owner_token,
            )
        except RedisError:
            logger.exception(
                "failed to release guard after durable action setup failure",
                extra={
                    "confirmation_id": confirmation_id,
                    "conversation_id": deps.conversation_id,
                },
            )
    deps.guard_handoff = False

    try:
        async with AsyncSessionLocal() as log_db:
            async with log_db.begin():
                await operation_log_service.mark_expired_if_pending(log_db, log_id)
    except Exception:
        logger.exception(
            "failed to terminalize operation after durable action setup failure",
            extra={"confirmation_id": confirmation_id, "log_id": log_id},
        )

    if is_write_tool(registered.meta):
        try:
            await decr_quota(
                redis_client,
                deps.user.user_id,
                agent_code=agent_code_for_rollback,
                l1_member=l1_member,
                l1_global_member=l1_global_member,
                l4_conv_key=l4_conv_key_for_rollback,
            )
        except RedisError:
            logger.exception(
                "failed to roll back quota after durable action setup failure",
                extra={"confirmation_id": confirmation_id},
            )


async def _hang_for_confirmation(
    deps: ChatDeps,
    registered: RegisteredTool,
    log_id: int,
    tool_call_id: str,
    args: dict[str, Any],
    summary: str,
    dry_run_summary: DryRunSummary | None,
    *,
    prepared_action_context: _PreparedExecutionContext | None = None,
    agent_code_for_rollback: str | None = None,
    l1_member: str | None = None,
    l1_global_member: str | None = None,
    l4_conv_key_for_rollback: str | None = None,
) -> _ConfirmationResolution | None:
    """HITL 流：create_pending → emit confirmation_required → hang

    Returns:
        ConfirmAction.APPROVED / REJECTED — 用户确认结果
        None — 已 expired（5min TTL 超时，log 已迁移到 EXPIRED）
    """
    meta = registered.meta
    execution_args = args
    if (
        prepared_action_context is None
        and dry_run_summary is not None
        and dry_run_summary.execution_args is not None
    ):
        execution_args = dry_run_summary.execution_args
    confirmation_id = hitl_manager.generate_confirmation_id()
    payload = await hitl_manager.create_pending(
        redis_client,
        confirmation_id=confirmation_id,
        user_id=deps.user.user_id,
        tenant_id=deps.tenant_id,
        conversation_id=deps.conversation_id or 0,
        tool_call_id=tool_call_id,
        trace_id=deps.trace_id,
        tool_name=meta.name,
        args=execution_args,
        dry_run_result=_summary_to_dict(dry_run_summary),
        source_user_message_id=deps.source_user_message_id,
        guard_owner_token=deps.guard_owner_token,
        command_action=deps.command_action,
        agent_code=deps.agent.code if deps.agent else None,
        risk_level=meta.risk,
        chip_target=meta.chip_target,
    )

    if deps.guard_owner_token and deps.conversation_id:
        from app.modules.ai.service.chat_run_service import (  # noqa: PLC0415
            chat_run_guard,
        )

        handed_off = await chat_run_guard.handoff_pending(
            redis_client,
            conversation_id=deps.conversation_id,
            owner_token=deps.guard_owner_token,
            confirmation_ttl_sec=settings.AI_HITL_PENDING_TTL_SEC,
        )
        if not handed_off:
            await _rollback_failed_confirmation_setup(
                deps,
                registered,
                confirmation_id,
                log_id,
                agent_code_for_rollback=agent_code_for_rollback,
                l1_member=l1_member,
                l1_global_member=l1_global_member,
                l4_conv_key_for_rollback=l4_conv_key_for_rollback,
            )
            return None
    deps.guard_handoff = True

    # 回填 confirmation_id 到 log 行，并让所有新 HITL 共用 PostgreSQL action。
    durable_action: AiPreparedAction | None = None
    try:
        async with AsyncSessionLocal() as log_db:
            async with log_db.begin():
                await operation_log_service.attach_confirmation(
                    log_db, log_id, confirmation_id
                )
                pending_expires_at = datetime.fromisoformat(
                    payload.expires_at.replace("Z", "+00:00")
                )
                if prepared_action_context is not None:
                    proposal = prepared_action_context.proposal
                    proposal_expires_at = proposal.expires_at
                    if proposal_expires_at.tzinfo is None:
                        proposal_expires_at = proposal_expires_at.replace(tzinfo=UTC)
                    durable_action = await prepared_action_service.create_pending(
                        log_db,
                        confirmation_id=confirmation_id,
                        prepare_tool_call_id=(
                            prepared_action_context.prepare_tool_call_id
                        ),
                        prepare_tool_name=prepared_action_context.prepare_tool_name,
                        execute_tool_call_id=tool_call_id,
                        execute_tool_name=meta.name,
                        frozen_args=proposal.frozen_args,
                        snapshot=proposal.snapshot,
                        snapshot_hash=proposal.snapshot_hash,
                        subject_ref=proposal.subject_ref,
                        presentation=proposal.presentation,
                        user_id=deps.user.user_id,
                        tenant_id=deps.tenant_id,
                        conversation_id=deps.conversation_id or 0,
                        source_user_message_id=deps.source_user_message_id or 0,
                        trace_id=deps.trace_id,
                        agent_code=deps.agent.code if deps.agent else meta.agent,
                        expires_at=min(proposal_expires_at, pending_expires_at),
                        resolved_model_id=deps.resolved_model_id,
                        resolved_provider_id=deps.resolved_provider_id,
                        data_scope_hash=deps.data_scope_hash,
                        projection_kind=meta.projection_kind,
                        guard_owner_token=deps.guard_owner_token,
                        command_action=deps.command_action,
                        risk_level=meta.risk,
                        chip_target=meta.chip_target,
                        projection_dependency_message_ids=(
                            deps.projection_dependency_message_ids
                        ),
                        require_live_source=True,
                    )
                else:
                    direct_snapshot = {
                        "tool": meta.name,
                        "argsHash": compute_args_hash(execution_args),
                        "dryRun": _summary_to_dict(dry_run_summary),
                    }
                    if (
                        dry_run_summary is not None
                        and dry_run_summary.business_snapshot is not None
                    ):
                        direct_snapshot["business"] = dry_run_summary.business_snapshot
                    fields = _build_direct_confirmation_fields(
                        meta, args, dry_run_summary
                    )
                    durable_action = await prepared_action_service.create_pending(
                        log_db,
                        confirmation_id=confirmation_id,
                        prepare_tool_call_id=None,
                        prepare_tool_name=None,
                        execute_tool_call_id=tool_call_id,
                        execute_tool_name=meta.name,
                        frozen_args=execution_args,
                        snapshot=direct_snapshot,
                        snapshot_hash="",
                        subject_ref=None,
                        presentation={
                            "title": meta.name,
                            "summary": (
                                dry_run_summary.summary
                                if dry_run_summary is not None
                                else meta.summary
                            ),
                            "summaryKey": (
                                dry_run_summary.summary_key
                                if dry_run_summary is not None
                                else None
                            ),
                            "summaryParams": (
                                dry_run_summary.summary_params or {}
                                if dry_run_summary is not None
                                else {}
                            ),
                            "fields": fields,
                            "warnings": (
                                ["此操作不可逆，请确认影响范围。"]
                                if meta.risk == "destructive"
                                else []
                            ),
                            "warningKeys": (
                                ["page.ai.chat.destructiveWarning"]
                                if meta.risk == "destructive"
                                else []
                            ),
                        },
                        interaction_flow="direct",
                        requested_outcome="direct",
                        user_id=deps.user.user_id,
                        tenant_id=deps.tenant_id,
                        conversation_id=deps.conversation_id or 0,
                        source_user_message_id=deps.source_user_message_id or 0,
                        trace_id=deps.trace_id,
                        agent_code=deps.agent.code if deps.agent else meta.agent,
                        expires_at=pending_expires_at,
                        resolved_model_id=deps.resolved_model_id,
                        resolved_provider_id=deps.resolved_provider_id,
                        data_scope_hash=(
                            deps.data_scope_hash
                            if meta.projection_kind == "scope_bound"
                            else None
                        ),
                        projection_kind=meta.projection_kind,
                        guard_owner_token=deps.guard_owner_token,
                        command_action=deps.command_action,
                        risk_level=meta.risk,
                        chip_target=meta.chip_target,
                        projection_dependency_message_ids=(
                            deps.projection_dependency_message_ids
                        ),
                        require_live_source=True,
                    )
                if durable_action is not None:
                    await operation_log_service.attach_target_summary(
                        log_db,
                        log_id,
                        durable_action.subject_refs,
                    )
                    payload = await hitl_manager.bind_durable_action(
                        redis_client,
                        confirmation_id,
                        durable_action.action_id,
                    )
    except BaseException:
        await _rollback_failed_confirmation_setup(
            deps,
            registered,
            confirmation_id,
            log_id,
            agent_code_for_rollback=agent_code_for_rollback,
            l1_member=l1_member,
            l1_global_member=l1_global_member,
            l4_conv_key_for_rollback=l4_conv_key_for_rollback,
        )
        raise

    if durable_action is None:  # pragma: no cover - fail-closed invariant
        raise RuntimeError("durable HITL action was not created")

    # 通知客户端展示确认界面。
    await _emit(
        deps,
        ConfirmationRequiredEvent(
            confirmation_id=confirmation_id,
            tool=meta.name,
            tool_call_id=tool_call_id,
            summary=summary,
            expires_at=payload.expires_at,
            action_id=durable_action.action_id,
            source_tool_call_id=(
                prepared_action_context.prepare_tool_call_id
                if prepared_action_context
                else None
            ),
            interaction_flow=durable_action.interaction_flow,
            presentation=durable_action.presentation,
            dry_run=dry_run_summary,
        ),
    )

    # 阻塞等待批准、拒绝或超时唤醒。
    try:
        action = await hitl_manager.hang(confirmation_id)
    except TimeoutError:
        terminal = await _get_prepared_action_by_confirmation(confirmation_id)
        if terminal is not None and PreparedActionStatus(terminal.status).is_terminal:
            deps.guard_handoff = False
            return _ConfirmationResolution(
                confirmation_id=confirmation_id,
                decision=(
                    ConfirmAction.REJECTED
                    if terminal.status == PreparedActionStatus.REJECTED.value
                    else ConfirmAction.APPROVED
                ),
            )
        # 5min TTL 超时 → mark_expired
        async with AsyncSessionLocal() as log_db:
            async with log_db.begin():
                transitioned = await prepared_action_service.transition_status(
                    log_db,
                    action_id=durable_action.action_id,
                    expected_status=PreparedActionStatus.PENDING_CONFIRMATION,
                    expected_version=durable_action.row_version,
                    target_status=PreparedActionStatus.EXPIRED,
                    error_code="AI_HITL_EXPIRED",
                )
                if transitioned is not None:
                    await operation_log_service.mark_expired_if_pending(log_db, log_id)
                    from app.modules.ai.service.chat_run_service import (  # noqa: PLC0415
                        chat_run_finalizer,
                    )

                    await chat_run_finalizer.finalize_prepared_action(
                        log_db,
                        action=transitioned,
                        ok=False,
                        error_code="AI_HITL_EXPIRED",
                        error_msg="确认已过期，请重新发起",
                    )
        await hitl_manager.delete_pending(redis_client, confirmation_id)
        deps.guard_handoff = False
        return None
    else:
        deps.guard_handoff = False

    # 用户确认后清 Redis pending
    await hitl_manager.delete_pending(redis_client, confirmation_id)
    return _ConfirmationResolution(
        confirmation_id=confirmation_id,
        decision=action,
    )


def _build_direct_confirmation_fields(
    meta: AiToolMeta,
    args: dict[str, Any],
    dry_run_summary: DryRunSummary | None,
) -> list[dict[str, Any]]:
    """合并原始参数与已绑定的 dry-run 展示值，不修改冻结执行参数。"""
    enriched_by_label: dict[str, dict[str, Any]] = {}
    allowed_labels = set(meta.args_summary_fields)
    if dry_run_summary is not None:
        for field in dry_run_summary.confirmation_fields or []:
            label = field.get("label")
            if not isinstance(label, str) or label not in allowed_labels:
                raise ValueError("confirmation field label is not a frozen argument")
            if label in enriched_by_label:
                raise ValueError("confirmation field label must be unique")
            raw_value = field.get("value")
            frozen_value = args.get(label)
            if type(raw_value) is not type(frozen_value) or raw_value != frozen_value:
                raise ValueError(
                    "confirmation field value does not match frozen argument"
                )
            enriched_by_label[label] = {
                "label": label,
                "value": field.get("display_value", raw_value),
            }

    fields: list[dict[str, Any]] = []
    for field_name in meta.args_summary_fields:
        value = args.get(field_name)
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            fields.append(
                enriched_by_label.pop(
                    field_name,
                    {"label": field_name, "value": value},
                )
            )

    fields.extend(enriched_by_label.values())
    if dry_run_summary is not None:
        fields.append(
            {
                "label": "affectedCount",
                "value": dry_run_summary.affected_count,
                "tone": "warning",
            }
        )
    return fields


def _summary_to_dict(s: DryRunSummary | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {
        "summary": s.summary,
        "affected_count": s.affected_count,
        "summary_key": s.summary_key,
        "summary_params": s.summary_params,
        "affected_examples": s.affected_examples,
    }


# ============ 业务执行（独立 session + L3 超时 + 脱敏） ============


async def _invoke_tool_fn(
    registered: RegisteredTool,
    args: dict[str, Any],
    deps: ChatDeps,
    args_hash: str,
    *,
    agent_code_for_rollback: str | None = None,
    l1_member: str | None = None,
    l1_global_member: str | None = None,
    l4_conv_key_for_rollback: str | None = None,
    approved_business_snapshot: dict[str, Any] | None = None,
) -> ToolResult:
    """在独立数据库会话内调用业务函数。

    授权或数据权限失败时回滚已经写入的用户、Agent、全局和会话配额，
    避免拒绝请求消耗用户额度。
    """
    meta = registered.meta
    tool_fn = registered.fn
    user_id = deps.user.user_id

    try:
        async with AsyncSessionLocal() as tool_db:
            async with tool_db.begin():
                tool_ctx = build_tool_context(
                    deps,
                    tool_db,
                    meta,
                    approved_business_snapshot=approved_business_snapshot,
                )
                # L3 单 tool 超时包装
                raw = await with_l3_timeout(tool_fn(tool_ctx, **args))
                # 同时兼容完整 ToolResult 和第三方工具返回的裸值。
                if isinstance(raw, ToolResult):
                    # 业务方已构造完整 ToolResult（builtin tool 新风格）
                    # 仍要脱敏 data 字段（ui 不脱敏，不进 LLM）
                    raw.data = serialize_for_llm(meta.sensitive_output, raw.data)
                    result = raw
                else:
                    # 业务方返回裸 dict / list / 标量（第三方 tool / 老代码 / fallback）
                    safe_data = serialize_for_llm(meta.sensitive_output, raw)
                    result = ToolResult.success(
                        data=safe_data
                    )  # ui=None，前端 fallback
                if result.projection is None and meta.projection_kind is not None:
                    result.projection = ResultProjection(
                        scope_bound=meta.projection_kind == "scope_bound"
                    )
                # 成功后清零相同参数的连续失败计数。
                await clear_failures(redis_client, user_id, meta.name, args_hash)
                # 只读工具缓存白名单筛选条件，供结果卡跳转后恢复页面查询。
                cache_module = meta.chip_target or meta.query_cache_module
                if meta.readonly and cache_module and deps.trace_id:
                    _safe_write_query_cache(
                        meta,
                        args,
                        deps,
                        user_id,
                        projection=result.projection,
                        module=cache_module,
                    )
                return result
    except AuthorizationException as e:
        # 授权失败不消耗额度，回滚此前成功写入的所有配额层级。
        if is_write_tool(meta):
            try:
                await decr_quota(
                    redis_client,
                    user_id,
                    agent_code=agent_code_for_rollback,
                    l1_member=l1_member,
                    l1_global_member=l1_global_member,
                    l4_conv_key=l4_conv_key_for_rollback,
                )
            except RedisError:
                # 回滚失败不阻断主流程（业务已失败，配额少 1 是次要问题）
                logger.exception(
                    "quota decr failed on AuthorizationException",
                    extra={"user_id": user_id, "tool": meta.name},
                )
        # 异步记录来源 IP 的鉴权拒绝。
        await _record_perm_denied_for_ip(deps, meta.name)
        # 授权失败不是工具业务失败，不计入连续失败统计。
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
    meta: Any,
    args: dict[str, Any],
    deps: ChatDeps,
    user_id: int,
    *,
    projection: ResultProjection | None = None,
    module: str | None = None,
) -> None:
    """写查询回放缓存，且只保存 allowed_filters 白名单字段。

    失败静默——query_cache 是辅助功能，不能让 chip 跳转失败影响主流程。
    异步调度避免阻塞 LLM 响应。

    module 参数优先，未传时回退到工具元数据中的兼容字段。
    """
    import asyncio  # noqa: PLC0415

    from app.modules.ai.agents.hitl.query_cache import (  # noqa: PLC0415
        set_query_cache,
    )

    cache_module = module or meta.query_cache_module
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
                module=cache_module,
                filters=safe_filters,
                user_id=user_id,
                tenant_id=deps.tenant_id,
                agent_code=deps.agent.code if deps.agent else meta.agent,
                projection=projection,
                data_scope_hash=deps.data_scope_hash,
                projection_dependency_message_ids=(
                    deps.projection_dependency_message_ids
                ),
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


# ============ IP 鉴权拒绝计数 ============


async def _record_perm_denied_for_ip(deps: ChatDeps, tool_name: str) -> None:
    """鉴权拒绝时异步记录来源 IP。

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


# ============ SSE 续传业务执行 ============


async def resume_tool_execution(
    pending: PendingPayload,
    deps: ChatDeps,
    log_id: int,
) -> tuple[ToolResult, int]:
    """续传端点专用：从 pending payload 重建执行上下文，跑业务函数

    与 execute_tool 的区别：
      - 不重做 perm/quota/dry_run/log_start（首次 execute_tool 已做，避免双扣 quota / 双写 log）
      - 不 emit tool_call_started（首次已发）
      - 不 emit tool_call_result（resume 端点自己 emit，让 SSE 顺序连续）
      - 写 log 终态（success/failed）

    Args:
        pending: Redis pending payload（含 tool_name / args / trace_id）
        deps: 续传时重建的 ChatDeps（含 user / perms / agent）
        log_id: 首次 execute_tool 写的 ai_operation_log.log_id

    Returns:
        (ToolResult, duration_ms) — resume 端点据此 emit tool_call_result，
        duration_ms 是业务执行墙钟耗时（毫秒）。
    """
    registry = ToolRegistry.get()
    registered = registry.find(pending.tool_name)
    if registered is None:
        logger.error(
            "resume: tool not found",
            extra={"tool": pending.tool_name, "tool_call_id": pending.tool_call_id},
        )
        return (
            ToolResult.failure(
                error_code="AI_TOOL_NOT_FOUND",
                error_msg=USER_FACING_MSG["AI_TOOL_NOT_FOUND"],
            ),
            0,
        )

    meta = registered.meta
    if (
        deps.agent is None
        or not getattr(deps.agent, "enabled", False)
        or getattr(deps.agent, "code", None) != meta.agent
    ):
        return (
            ToolResult.failure(
                error_code="AI_TOOL_AGENT_MISMATCH",
                error_msg=USER_FACING_MSG["AI_TOOL_AGENT_MISMATCH"],
            ),
            0,
        )
    if not set(meta.required_perms) <= deps.perms:
        return (
            ToolResult.failure(
                error_code="AI_TOOL_PERM_DENIED",
                error_msg=USER_FACING_MSG["AI_TOOL_PERM_DENIED"],
            ),
            0,
        )
    if meta.super_admin_only and not is_super_admin(deps.user):
        return (
            ToolResult.failure(
                error_code="AI_SUPER_ADMIN_REQUIRED",
                error_msg="此操作仅超级管理员可执行，请联系超管或走传统界面",
            ),
            0,
        )

    args_hash = compute_args_hash(pending.args)
    started_at = time.monotonic()
    result = await _invoke_tool_fn(
        registered, pending.args, deps, args_hash, l1_member=None
    )
    duration_ms = int((time.monotonic() - started_at) * 1000)
    await _finish_log_final(log_id, result, started_at)
    return result, duration_ms
