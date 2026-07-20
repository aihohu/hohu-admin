"""Agent 工厂

创建 Pydantic AI Agent 实例。

spec §17.2：从 system_tools.register_system_tools(agent) 模式迁移到
ToolRegistry + build_pydantic_ai_tools（声明式装饰器）。
spec §7.6：system prompt 用 build_system_prompt 三段拼接（SAFETY_PREAMBLE +
agent.system_prompt + dynamic_block），不再硬编码 instruction。
"""

from typing import Any

from pydantic_ai import Agent

from app.modules.ai.agents.safety_preamble import build_system_prompt
from app.modules.ai.agents.tools.pydantic_ai_wrapper import build_pydantic_ai_tools
from app.modules.ai.core.config import ChatDeps  # noqa: F401  向后兼容 re-export
from app.modules.ai.core.context import ChatDeps as NewChatDeps


def create_chat_agent(
    model: Any,
    *,
    user_perms: set[str] | None = None,
    agent_code: str = "user_mgmt",
    enabled_extra: list[str] | None = None,
) -> Agent:
    """创建对话 Agent

    Args:
        model: Pydantic AI Model 实例
        user_perms: 用户权限码集合，用于按 Agent + perms 过滤 tool（spec §5.4）
                    None 表示用所有内置 tool（向后兼容旧 ChatDeps 调用方）
        enabled_extra: v1.5+ SR-17 sys_config.ai:enabled_tools 解析结果，
                       None=不做 default_enabled 过滤（向后兼容）

    Returns:
        配置好工具的 Agent 实例

    system prompt（spec §7.6）：
        用动态 instructions（Callable[[RunContext[ChatDeps]], str]），
        每次推理时从 ctx.deps 重新构造（保证 data_scope / 时间 / trace_id 实时）。
        agent.system_prompt 字段在 chat_service.build_chat_deps 加载时已设到 deps.agent。
    """
    # 默认 perms=所有权限（过渡期兼容旧 chat.py 调用）
    # 1.5 完成后由 chat.py 显式传入从 user 加载的真实 perms
    effective_perms = user_perms if user_perms is not None else _all_registry_perms()

    # v1.5+: agent_code 决定可见 tool 集合（spec §5.4）
    # v1.5+ SR-17: enabled_extra 来自 sys_config.ai:enabled_tools，控制 default_enabled=False 的 tool
    tools = build_pydantic_ai_tools(
        effective_perms, agent_code, enabled_extra=enabled_extra
    )

    def instructions(ctx) -> str:
        """动态 system prompt（每轮推理时重新构造）"""
        deps = ctx.deps
        agent_system_prompt = getattr(deps.agent, "system_prompt", "") or ""
        return build_system_prompt(agent_system_prompt, deps)

    agent = Agent(
        model,
        deps_type=NewChatDeps,
        tools=tools,
        instructions=instructions,
    )

    return agent


def _all_registry_perms() -> set[str]:
    """调试 / 过渡用：返回 Registry 中所有 tool 的 required_perms 并集"""
    from app.modules.ai.agents.tools.registry import (  # noqa: PLC0415  避免 chat_agent 与 tools/__init__ 循环
        ToolRegistry,
    )

    perms: set[str] = set()
    for t in ToolRegistry.get().all():
        perms.update(t.meta.required_perms)
    return perms
