"""Agent 工厂

创建 Pydantic AI Agent 实例，注册工具。
"""

from pydantic_ai import Agent

from app.modules.ai.agents.tools.system_tools import register_system_tools
from app.modules.ai.core.config import ChatDeps


def create_chat_agent(model) -> Agent:
    """创建对话 Agent

    Args:
        model: Pydantic AI Model 实例

    Returns:
        配置好工具的 Agent 实例
    """
    agent = Agent(
        model,
        deps_type=ChatDeps,
        instructions=(
            "你是 hohu 管理平台的 AI 助手。你可以查询系统数据、回答管理相关问题。\n"
            "回答时使用中文，简洁明了。如果需要查询系统数据，请使用提供的工具。"
        ),
    )

    # 注册系统查询工具
    register_system_tools(agent)

    return agent
