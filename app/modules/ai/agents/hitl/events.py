"""AI 工具调用使用的 SSE 事件模型。

后端 → 前端 SSE 帧协议（前端 src/typings/api/ai.d.ts 对应）：
  - tool_call_started: tool 开始执行（autonomous / HITL 唤醒后都发）
  - tool_call_result: tool 执行结束（成功带 result，失败带 error_code/error_msg）
  - confirmation_required: HITL 触发，前端弹抽屉
  - ai_error: 流级错误（如 LLM API 故障）
  - done: 流结束

text-delta / reasoning-delta 走 Vercel UI Protocol v4（`data: {"type":"text-delta",...}`），
后端不发。前端按 `\n\n` 切 SSE 帧，每帧解析 `data: (.*)`。

**字段命名（camelCase 决策）**：SSE 自定义事件 JSON 顶层字段全部 camelCase
（如 `toolCallId` / `durationMs`），与项目其他 API 响应命名一致。
唯一例外是 `args` 内部 — LLM schema 参数定义保持 snake_case（与 ToolFn 签名一致），
不转 camelCase，避免 LLM 看到的 tool schema 与前端透传 args 形态对不上。
"""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic.experimental.missing_sentinel import MISSING

if TYPE_CHECKING:
    from app.modules.ai.agents.gateway.result import ResultProjection, UIResult


@dataclass(frozen=True)
class ToolCallStartedEvent:
    """tool 开始执行事件

    risk 为 low / high / destructive，前端用来渲染色条、
    chip 标签 + 状态文本。

    trace_id 用于结果 chip 跳转：readonly tool 成功后前端用 trace_id 调
    /ai/query-cache/<trace_id> 拿到 filters 回放到业务模块页。

    chip_target: readonly tool 的声明式 chip 跳转路径，
    替代前端 CHIP_TARGETS map）。None 表示无 chip。
    """

    tool: str
    tool_call_id: str
    summary: str
    args: dict[str, Any]
    risk: Literal["low", "high", "destructive"]
    trace_id: str
    chip_target: str | None = None
    type: Literal["tool_call_started"] = "tool_call_started"


@dataclass(frozen=True)
class ToolCallResultEvent:
    """tool 执行结束事件

    duration_ms: 从 started_at 到 emit 此事件的实际墙钟耗时（毫秒），含 HITL
    等待时间。前端展示「已执行 · 230ms」。
    affected_rows: 影响行数推断值（dry_run_count 优先；否则从 result 推断），
    None 表示无法推断，前端不展示「N 行」尾部。

    ui: UI 层结果，前端按 ui.view_type 路由标准组件。
    None（ok=False / 业务方未填 / executor fallback）→ 前端 fallback 到 plain_json。
    不进 LLM context（executor 内 strip）。
    """

    tool: str
    tool_call_id: str
    ok: bool
    duration_ms: int
    result: Any = None
    affected_rows: int | None = None
    error_code: str | None = None
    error_msg: str | None = None
    ui: "UIResult | None" = None
    projection: "ResultProjection | None" = None
    type: Literal["tool_call_result"] = "tool_call_result"


@dataclass(frozen=True)
class DryRunSummary:
    """HITL 确认抽屉展示的影响范围。"""

    summary: str
    affected_count: int
    summary_key: str | None = None
    summary_params: dict[str, str | int | float] | None = None
    affected_examples: list[str] | None = None
    confirmation_fields: list[dict[str, Any]] | None = None
    # Internal-only authorization data. event_to_sse_data deliberately omits these
    # fields so exact execution bindings and snapshots never become client input.
    execution_args: dict[str, Any] | None = None
    business_snapshot: dict[str, Any] | None = None


@dataclass(frozen=True)
class ConfirmationRequiredEvent:
    """HITL 触发事件，前端弹 chat-confirmation-drawer"""

    confirmation_id: str
    tool: str
    tool_call_id: str
    summary: str
    expires_at: str  # ISO 8601 UTC，e.g. "2026-07-02T14:07:30Z"
    dry_run: DryRunSummary | None = None
    action_id: int | None = None
    source_tool_call_id: str | None = None
    interaction_flow: Literal["direct", "prepared"] = "direct"
    presentation: dict[str, Any] | None = None
    type: Literal["confirmation_required"] = "confirmation_required"


@dataclass(frozen=True)
class ConfirmationResumedEvent:
    """SSE 续传重连事件。

    schema 与 ConfirmationRequiredEvent 兼容（前端可统一渲染），仅多 resumedAt
    字段用于"已重连"UI badge。前端收到此事件后：
      - 用 confirmationId / toolCallId 反查 / 重建 HITL 抽屉
      - 显示"已重连"chip（区别于首次 confirmation_required）
    """

    confirmation_id: str
    tool: str
    tool_call_id: str
    summary: str
    expires_at: str
    resumed_at: str
    dry_run: DryRunSummary | None = None
    action_id: int | None = None
    source_tool_call_id: str | None = None
    interaction_flow: Literal["direct", "prepared"] = "direct"
    presentation: dict[str, Any] | None = None
    type: Literal["confirmation_resumed"] = "confirmation_resumed"


@dataclass(frozen=True)
class AiErrorEvent:
    """流级错误事件（如 LLM API 故障 / 工具链严重错误）"""

    error_code: str
    message: str
    type: Literal["ai_error"] = "ai_error"


@dataclass(frozen=True)
class DoneEvent:
    """流结束事件"""

    trace_id: str | None = None
    message_id: int | None = None
    persistence: Literal["committed", "failed", "not_applicable"] | None = None
    projection: Literal["updated", "unchanged"] | None = None
    type: Literal["done"] = "done"


@dataclass(frozen=True)
class ClarificationRequiredEvent:
    """无状态澄清事件：前端展示候选卡片，不生成 confirmationId。

    与 ConfirmationRequiredEvent 区别：
      - ConfirmationRequiredEvent：HITL tool 确认（带 confirmationId + expiresAt + Redis）
      - ClarificationRequiredEvent：Agent 路由模糊（无状态，前端重发即可）
    """

    candidates: tuple[dict, ...]
    """({"code": "user_mgmt", "name": "...", "description": "..."}, ...)"""

    message: str
    reason_code: str | None = None
    type: Literal["clarification_required"] = "clarification_required"


AiStreamEvent = (
    ToolCallStartedEvent
    | ToolCallResultEvent
    | ConfirmationRequiredEvent
    | ConfirmationResumedEvent
    | ClarificationRequiredEvent
    | AiErrorEvent
    | DoneEvent
)
"""所有 SSE 自定义事件的联合类型"""


def event_to_sse_data(event: AiStreamEvent) -> str:
    """把事件序列化为 SSE `data: {...}` 行的 payload 字符串

    自定义事件 JSON 序列化后放在 ``data:`` 后，前端解析即可得到
    camelCase keys 的对象。

    Args:
        event: 任意 AiStreamEvent 子类

    Returns:
        JSON 字符串（None 字段递归移除，保留 False / 0 / 空字符串）
    """
    if isinstance(event, ToolCallStartedEvent):
        payload: dict[str, Any] = {
            "type": event.type,
            "tool": event.tool,
            "toolCallId": event.tool_call_id,
            "summary": event.summary,
            "args": event.args,  # snake_case 保留（LLM 参数命名）
            "risk": event.risk,
            "traceId": event.trace_id,
            "chipTarget": event.chip_target,
        }
    elif isinstance(event, ToolCallResultEvent):
        payload = {
            "type": event.type,
            "tool": event.tool,
            "toolCallId": event.tool_call_id,
            "ok": event.ok,
            "result": event.result,
            "errorCode": event.error_code,
            "errorMsg": event.error_msg,
            "durationMs": event.duration_ms,
            "affectedRows": event.affected_rows,
            "ui": _ui_to_dict(event.ui),
        }
    elif isinstance(event, ConfirmationRequiredEvent):
        payload = {
            "type": event.type,
            "confirmationId": event.confirmation_id,
            "tool": event.tool,
            "toolCallId": event.tool_call_id,
            "summary": event.summary,
            "expiresAt": event.expires_at,
            "dryRun": _dry_run_to_dict(event.dry_run),
            "actionId": event.action_id,
            "sourceToolCallId": event.source_tool_call_id,
            "interactionFlow": event.interaction_flow,
            "presentation": event.presentation,
        }
    elif isinstance(event, ConfirmationResumedEvent):
        payload = {
            "type": event.type,
            "confirmationId": event.confirmation_id,
            "tool": event.tool,
            "toolCallId": event.tool_call_id,
            "summary": event.summary,
            "expiresAt": event.expires_at,
            "resumedAt": event.resumed_at,
            "dryRun": _dry_run_to_dict(event.dry_run),
            "actionId": event.action_id,
            "sourceToolCallId": event.source_tool_call_id,
            "interactionFlow": event.interaction_flow,
            "presentation": event.presentation,
        }
    elif isinstance(event, ClarificationRequiredEvent):
        payload = {
            "type": event.type,
            "candidates": list(event.candidates),
            "message": event.message,
            "reasonCode": event.reason_code,
        }
    elif isinstance(event, AiErrorEvent):
        payload = {
            "type": event.type,
            "errorCode": event.error_code,
            "message": event.message,
        }
    elif isinstance(event, DoneEvent):
        payload = {
            "type": event.type,
            "traceId": event.trace_id,
            "messageId": event.message_id,
            "persistence": event.persistence,
            "projection": event.projection,
        }
    else:  # pragma: no cover — 类型穷尽后 unreachable
        raise ValueError(f"unknown event type: {type(event).__name__}")

    return _compact_json(payload)


def _dry_run_to_dict(s: DryRunSummary | None) -> dict[str, Any] | None:
    """DryRunSummary 转 camelCase dict（None 字段交给 _compact_json 移除）"""
    if s is None:
        return None
    return {
        "summary": s.summary,
        "affectedCount": s.affected_count,
        "summaryKey": s.summary_key,
        "summaryParams": s.summary_params,
        "affectedExamples": s.affected_examples,
    }


def _ui_to_dict(ui: "UIResult | None") -> dict[str, Any] | None:
    """UIResult 转 camelCase dict（None 字段交给 _compact_json 移除）"""
    if ui is None:
        return None
    return {
        "viewType": ui.view_type,
        "viewData": ui.view_data,
        "audit": ui.audit,
        "labelKey": ui.label_key,
        "labelParams": ui.label_params,
    }


JS_MAX_SAFE_INT = 1 << 53  # 9007199254740992 — JS Number.MAX_SAFE_INTEGER + 1


def stringify_large_ints(v: Any) -> Any:
    """递归把 abs(int) >= 2^53 的整数转成 str（防 JS Number 精度丢失）。

    CLAUDE.md 跨项目硬规则 #3：Snowflake ID 是 int64，超过 JS
    Number.MAX_SAFE_INTEGER (2^53-1)，JSON 序列化为 number 时前端
    JSON.parse 会丢失末几位精度（如 7483433649145122816 → 7483433649145123000）。

    小整数（count / affected_rows / status code 等）保持 int 不变。
    bool 不是 int（Python 中 bool 是 int 子类，需显式排除）。

    用于 SSE 事件 args / result 序列化 + DB ai_message.tool_calls JSON 列。
    业务函数（dry_run_fn / tool_fn）仍接收原始 int args，不受影响。
    """
    if v is MISSING:
        return None
    if isinstance(v, dict):
        return {k: stringify_large_ints(vv) for k, vv in v.items() if vv is not MISSING}
    if isinstance(v, (list, tuple)):
        return [stringify_large_ints(x) for x in v if x is not MISSING]
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) >= JS_MAX_SAFE_INT:
        return str(v)
    return v


def _compact_json(data: Any) -> str:
    """递归移除 None 字段后 JSON dumps（保留 False / 0 / 空字符串）"""
    return json.dumps(
        _compact_value(data),
        ensure_ascii=False,
        default=str,
    )


def _compact_value(v: Any) -> Any:
    if v is MISSING:
        return None
    if isinstance(v, dict):
        return {
            k: _compact_value(vv)
            for k, vv in v.items()
            if vv is not None and vv is not MISSING
        }
    if isinstance(v, (list, tuple)):
        return [_compact_value(x) for x in v if x is not MISSING]
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) >= JS_MAX_SAFE_INT:
        return str(v)
    return v
