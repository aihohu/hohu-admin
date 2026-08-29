"""Agent 工厂

创建 Pydantic AI Agent 实例。

使用 ToolRegistry 和 ``build_pydantic_ai_tools`` 动态构造工具。
system prompt 由 ``build_system_prompt`` 拼接 SAFETY_PREAMBLE、
agent.system_prompt 和 dynamic_block，不再硬编码 instruction。
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
    user_perms: set[str],
    agent_code: str = "user_mgmt",
    enabled_extra: list[str] | None = None,
) -> Agent:
    """创建对话 Agent

    Args:
        model: Pydantic AI Model 实例
        user_perms: 用户显式权限码集合，用于按 Agent 和权限过滤工具
        enabled_extra: ``sys_config.ai:enabled_tools`` 的解析结果，
                       None=不做 default_enabled 过滤（向后兼容）

    Returns:
        配置好工具的 Agent 实例

    system prompt：
        用动态 instructions（Callable[[RunContext[ChatDeps]], str]），
        每次推理时从 ctx.deps 重新构造（保证 data_scope / 时间 / trace_id 实时）。
        agent.system_prompt 字段在 chat_service.build_chat_deps 加载时已设到 deps.agent。
    """
    # agent_code 决定可见工具集合。
    # enabled_extra 控制默认关闭工具的显式启用。
    tools = build_pydantic_ai_tools(user_perms, agent_code, enabled_extra=enabled_extra)

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
        model_settings={"parallel_tool_calls": False},
    )

    return agent
