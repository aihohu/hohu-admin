"""AI Tool Gateway — 装饰器 + Registry + Meta

按 spec docs/specs/2026-07-02-ai-tool-gateway-design.md §5。

典型用法：
    from app.modules.ai.agents.tools import AiToolMeta, ai_tool

    @ai_tool(AiToolMeta(
        name="user.create",
        agent="user_mgmt",
        summary="Create a new user account",
        required_perms=("system:user:add",),
        risk="high",
    ))
    async def create_user(ctx, username: str, dept_id: int):
        ...

启动校验：
    from app.modules.ai.agents.tools import ToolRegistry
    await ToolRegistry.get().validate_on_startup(db)
"""

from .decorator import ai_tool
from .meta import SENSITIVE_INPUT_BLOCKLIST, SHARED_AGENT_CODE, AiToolMeta
from .registry import (
    RegisteredTool,
    ToolRegistry,
    ToolRegistryError,
    compute_available_tools,
)
from .stats_validator import (
    validate_field_in_whitelist,
    validate_filters_in_whitelist,
    validate_group_by_in_whitelist,
)

__all__ = [
    "AiToolMeta",
    "RegisteredTool",
    "SENSITIVE_INPUT_BLOCKLIST",
    "SHARED_AGENT_CODE",
    "ToolRegistry",
    "ToolRegistryError",
    "ai_tool",
    "compute_available_tools",
    "load_builtin_tools",
    "validate_field_in_whitelist",
    "validate_filters_in_whitelist",
    "validate_group_by_in_whitelist",
]


# 注意：pydantic_ai_wrapper 故意不在顶部 import，避免循环：
#   context.py → tools/__init__.py → pydantic_ai_wrapper → context.py
# 使用时显式 from app.modules.ai.agents.tools.pydantic_ai_wrapper import ...


# ============ 启动扫描：触发各业务模块 @ai_tool 装饰器注册到 Registry ============
# spec §3：启动时扫描 @ai_tool 装饰器 → ToolRegistry（单例）
# 由 main.py lifespan 或测试 fixture 显式调用，避免在 import 期触发循环依赖。


def load_builtin_tools() -> None:
    """触发内置 tool 注册（spec §3 启动扫描）

    显式调用（不在 import 期触发），避免 system/ai_tools → context → User →
    db.base 的 import 链与 tools/__init__.py 自身形成循环。

    用法：
        # main.py lifespan
        from app.modules.ai.agents.tools import load_builtin_tools
        load_builtin_tools()
        await ToolRegistry.get().validate_on_startup(db)

        # 测试 fixture
        load_builtin_tools()
    """
    from importlib import import_module  # noqa: PLC0415  延迟 import 避免循环

    # Phase 1.4：system 模块的 user.count / user.stats / user.distinct
    import_module("app.modules.system.ai_tools")
