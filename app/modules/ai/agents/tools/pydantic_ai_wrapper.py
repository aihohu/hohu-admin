"""PydanticAI Tool 包装层 — Registry tool → PydanticAI Tool

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §5.1 / §6.3 / §8.2。

业务函数签名约定：
    async def fn(ctx: AiToolContext, **args) -> ...

PydanticAI 期望的签名：
    async def fn(ctx: RunContext[ChatDeps], **args) -> ...

本模块用 inspect.signature 动态构造 wrapper 签名：
  - 第一个参数 ctx 类型从 AiToolContext 替换为 RunContext[ChatDeps]
  - 其余参数保持原样（PydanticAI 据此生成 JSON schema 给 LLM）
  - wrapper 运行时**通过 Gateway Executor 路由**（spec §3 / §6）：
    perm check → 容量三层 → 风险分级 → HITL → 业务执行 → 脱敏 → 事件 emit

Phase 3.2 关键变化（vs Phase 1.2b）：
  - wrapper 不再直接调 original_fn（绕过 Gateway 的 critical gap）
  - wrapper 调 execute_tool，由 Executor 调 original_fn
  - ToolResult → LLM 友好字符串
"""

import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic_ai import RunContext, Tool

from app.modules.ai.agents.gateway.executor import execute_tool
from app.modules.ai.agents.gateway.result import ToolResult
from app.modules.ai.agents.tools.registry import (
    RegisteredTool,
    ToolRegistry,
    compute_available_tools,
)
from app.modules.ai.core.context import ChatDeps


def _build_wrapper_signature(
    original_fn: Callable[..., Awaitable[Any]],
    *,
    interaction_flow: Literal["direct", "prepared"] = "direct",
) -> inspect.Signature:
    """从原函数签名构造 wrapper 签名

    原函数: async def fn(ctx: AiToolContext, foo: str, bar: int = 0)
    wrapper : async def fn(ctx: RunContext[ChatDeps], foo: str, bar: int = 0)

    PydanticAI 据此生成 LLM 可见的 JSON schema（foo / bar 进 schema，
    ctx 不进；spec §7.2 sensitive_input 不进函数签名所以也不进 schema）。
    """
    orig_sig = inspect.signature(original_fn)
    orig_params = list(orig_sig.parameters.values())

    if not orig_params:
        raise ValueError(
            f"Tool function {original_fn.__qualname__} must accept ctx as first param"
        )

    # 第一个参数必须是 ctx（业务约定），替换类型注解
    new_ctx_param = inspect.Parameter(
        orig_params[0].name,
        kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
        annotation=RunContext[ChatDeps],
    )
    new_params = [new_ctx_param] + orig_params[1:]
    if interaction_flow == "prepared":
        requested_outcome = inspect.Parameter(
            "requested_outcome",
            kind=inspect.Parameter.KEYWORD_ONLY,
            annotation=Literal["preview_only", "execute_if_approved"],
        )
        var_keyword_index = next(
            (
                index
                for index, param in enumerate(new_params)
                if param.kind is inspect.Parameter.VAR_KEYWORD
            ),
            len(new_params),
        )
        new_params.insert(var_keyword_index, requested_outcome)
    return orig_sig.replace(parameters=new_params)


def _tool_result_to_llm_string(result: ToolResult) -> str:
    """把 ToolResult 序列化为 LLM 友好字符串

    LLM 看到的格式：
      success: JSON dump of data（如 {"count": 5}）
      failure: "[ToolError:CODE] msg"（让 LLM 知道是失败而非业务数据）
    """
    if result.ok:
        try:
            return json.dumps(result.data, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(result.data)
    return f"[ToolError:{result.error_code}] {result.error_msg}"


def wrap_tool_for_pydantic_ai(registered: RegisteredTool) -> Tool:
    """把 RegisteredTool 包装为 PydanticAI Tool

    Phase 3.2 工作流（spec §3 / §6 / §8.2）：
      1. PydanticAI 调 wrapper(ctx: RunContext[ChatDeps], **args)
      2. wrapper 调 execute_tool(meta.name, args, deps)
      3. Executor 内：perm → 容量 → HITL → 业务（独立 session）→ 脱敏
      4. Executor 返回 ToolResult
      5. wrapper 把 ToolResult → LLM 字符串返回给 PydanticAI
    """
    meta = registered.meta

    async def wrapper(ctx: RunContext[ChatDeps], **kwargs: Any) -> str:
        deps = ctx.deps
        result = await execute_tool(meta.name, kwargs, deps)
        return _tool_result_to_llm_string(result)

    # 注入动态签名（让 PydanticAI 推断正确的 JSON schema）
    wrapper.__signature__ = _build_wrapper_signature(  # type: ignore[attr-defined]
        registered.fn,
        interaction_flow=meta.interaction_flow,
    )

    # 同步 __annotations__（PydanticAI 用 typing.get_type_hints 读它推断参数类型）
    # 复制原函数 annotations，但把 ctx 类型替换为 RunContext[ChatDeps]
    orig_annotations = dict(getattr(registered.fn, "__annotations__", {}))
    orig_sig = inspect.signature(registered.fn)
    first_param_name = next(iter(orig_sig.parameters.keys()))
    orig_annotations[first_param_name] = RunContext[ChatDeps]
    if meta.interaction_flow == "prepared":
        orig_annotations["requested_outcome"] = Literal[
            "preview_only", "execute_if_approved"
        ]
    wrapper.__annotations__ = orig_annotations

    wrapper.__name__ = meta.name.replace(".", "_")
    wrapper.__doc__ = meta.summary

    # OpenAI 兼容 API（DeepSeek / 严格模式）要求 tool name 匹配 ^[a-zA-Z0-9_-]+$
    # 不允许点号；用下划线替换给 LLM 看，wrapper 内部仍用 meta.name 调 execute_tool
    llm_tool_name = meta.name.replace(".", "_")

    return Tool(
        function=wrapper,
        name=llm_tool_name,
        description=meta.summary,
    )


def build_pydantic_ai_tools(
    user_perms: set[str],
    agent_code: str,
    *,
    enabled_extra: list[str] | None = None,
) -> list[Tool]:
    """spec §5.4: 按 Agent + 用户 perms 过滤后，包装为 PydanticAI Tool 列表

    用法（chat_agent.py）:
        agent = Agent(model, deps_type=ChatDeps, tools=build_pydantic_ai_tools(perms, "user_mgmt"))

    Args:
        enabled_extra: v1.5+ SR-17 sys_config.ai:enabled_tools 解析结果，
            由 chat_service.create_agent 预 await 后传入（保持本函数同步）。

    返回顺序：按 Registry 注册顺序（稳定，便于调试）
    """
    registered_tools = compute_available_tools(
        user_perms, agent_code, enabled_extra=enabled_extra
    )
    return [wrap_tool_for_pydantic_ai(t) for t in registered_tools]


def get_registry_size() -> int:
    """调试用：返回 Registry 当前 tool 数量"""
    return len(ToolRegistry.get())
